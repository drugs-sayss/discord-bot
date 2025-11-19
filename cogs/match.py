import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from database.database import AsyncSessionLocal
from database.models import User, Team, TeamSlot, Card, Match, ActiveMatch, Bet, Leaderboard, Collection
from utils.embeds import EmbedBuilder
from utils.match_engine import MatchEngine, MatchState
from utils.redis_manager import redis_manager
from typing import Dict, Optional
import json
import logging

logger = logging.getLogger('discord_bot')

class PlayerSelectView(discord.ui.View):
    """View with select menu for picking players"""
    
    def __init__(self, match_state: MatchState, user_id: int, cog_instance, channel=None):
        super().__init__(timeout=300)  # 5 minute timeout
        self.match_state = match_state
        self.user_id = user_id
        self.cog = cog_instance
        self.channel = channel  # Store channel reference for DM responses
        self.created_for_round = match_state.current_round  # Track which round this view is for
        
        # Validate user_id is one of the players
        if user_id != match_state.player1_id and user_id != match_state.player2_id:
            logger.error(f"PlayerSelectView: Invalid user_id {user_id}. Player1: {match_state.player1_id}, Player2: {match_state.player2_id}")
            # Default to player1_team as fallback (shouldn't happen, but prevents crash)
            team = match_state.player1_team
            used_positions = match_state.player1_used_cards
            used_card_ids = match_state.player1_used_card_ids
        else:
            # Get the team directly - this is the source of truth
            if user_id == match_state.player1_id:
                team = match_state.player1_team
                used_positions = match_state.player1_used_cards
                used_card_ids = match_state.player1_used_card_ids
            else:
                team = match_state.player2_team
                used_positions = match_state.player2_used_cards
                used_card_ids = match_state.player2_used_card_ids

        # Build dropdown options directly from team, filtering out used cards
        # Use get_available_cards to ensure we're using the same logic everywhere
        available_cards = match_state.get_available_cards(user_id)
        
        # CRITICAL: Log available cards for debugging
        logger.info(f"Building dropdown for user {user_id}, round {match_state.current_round}. Available: {len(available_cards)} cards")
        if user_id == match_state.player1_id:
            logger.info(f"Player 1 used positions: {match_state.player1_used_cards}, used card IDs: {match_state.player1_used_card_ids}")
        else:
            logger.info(f"Player 2 used positions: {match_state.player2_used_cards}, used card IDs: {match_state.player2_used_card_ids}")

        options = []
        for position, card in available_cards.items():
            # Double-check the card hasn't been used (defensive programming)
            if position in used_positions:
                logger.error(f"CRITICAL: Position {position} found in available_cards but also in used_positions for user {user_id}. Skipping!")
                continue
            if card.id in used_card_ids:
                logger.error(f"CRITICAL: Card {card.name} (ID: {card.id}) found in available_cards but also in used_card_ids for user {user_id}. Skipping!")
                continue
                
            # This card is available - add to dropdown
            label = card.name[:100]  # Discord limit is 100 chars
            description = f"{position} - {card.attack_stat} ATK / {card.defense_stat} DEF"[:100]
            options.append(discord.SelectOption(
                label=label,
                description=description,
                value=position
            ))

        if options:
            select = discord.ui.Select(
                placeholder="Choose a player...",
                options=options[:25]  # Discord limit is 25 options
            )
            select.callback = self.on_select
            self.add_item(select)
        else:
            # No available cards - this shouldn't happen, but handle it
            select = discord.ui.Select(
                placeholder="No players available!",
                options=[discord.SelectOption(label="No players", value="none", description="All players used")]
            )
            select.callback = self.on_select
            self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        """Handle player selection"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ This menu is not for you!",
                ephemeral=True
            )
            return

        # Disable view immediately to prevent double-use
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except:
            pass  # DM might be deleted, ignore

        # Get the match channel (either from stored reference or current channel)
        match_channel = self.channel if self.channel else interaction.channel

        # CRITICAL: Get fresh match state from Redis/memory
        current_match_state = await self.cog._get_match_state(match_channel.id)
        if not current_match_state:
            await interaction.response.send_message(
                "❌ No active match found!",
                ephemeral=True
            )
            return

        # Update our reference to the fresh state
        self.match_state = current_match_state

        # CRITICAL: Verify this is for the current round (prevent stale view usage)
        if self.created_for_round != self.match_state.current_round:
            await interaction.response.send_message(
                f"❌ This menu is for Round {self.created_for_round}, but we're now on Round {self.match_state.current_round}!\n"
                f"Please use the latest menu sent to you.",
                ephemeral=True
            )
            return

        # Verify it's still this user's turn
        if self.match_state.current_turn != self.user_id:
            await interaction.response.send_message(
                "❌ It's not your turn anymore!",
                ephemeral=True
            )
            return

        selected_position = interaction.data['values'][0]

        # Get the card directly from the team - this is the source of truth
        if self.user_id == self.match_state.player1_id:
            team = self.match_state.player1_team
            used_positions = self.match_state.player1_used_cards
            used_card_ids = self.match_state.player1_used_card_ids
        else:
            team = self.match_state.player2_team
            used_positions = self.match_state.player2_used_cards
            used_card_ids = self.match_state.player2_used_card_ids

        # Verify the position exists in the team
        if selected_position not in team:
            await interaction.response.send_message(
                f"❌ Position {selected_position} is not in your team!",
                ephemeral=True
            )
            return

        # Get the card from the team
        selected_card = team[selected_position]

        # Verify the card hasn't been used
        if selected_position in used_positions:
            await interaction.response.send_message(
                "❌ This position has already been used in this match!",
                ephemeral=True
            )
            return

        if selected_card.id in used_card_ids:
            await interaction.response.send_message(
                f"❌ **{selected_card.name}** has already been used in this match!",
                ephemeral=True
            )
            return

        # Mark card as selected
        if not self.match_state.select_card(self.user_id, selected_position):
            await interaction.response.send_message(
                "❌ Failed to select this card. It may have already been used!",
                ephemeral=True
            )
            return

        # CRITICAL: Verify the card was actually marked as used
        if self.user_id == self.match_state.player1_id:
            if selected_position not in self.match_state.player1_used_cards:
                logger.error(f"CRITICAL BUG: Position {selected_position} was not added to player1_used_cards after select_card!")
            if selected_card.id not in self.match_state.player1_used_card_ids:
                logger.error(f"CRITICAL BUG: Card {selected_card.name} (ID: {selected_card.id}) was not added to player1_used_card_ids after select_card!")
        else:
            if selected_position not in self.match_state.player2_used_cards:
                logger.error(f"CRITICAL BUG: Position {selected_position} was not added to player2_used_cards after select_card!")
            if selected_card.id not in self.match_state.player2_used_card_ids:
                logger.error(f"CRITICAL BUG: Card {selected_card.name} (ID: {selected_card.id}) was not added to player2_used_card_ids after select_card!")

        # Log the selection for debugging
        logger.info(f"Player {self.user_id} selected card {selected_card.name} (ID: {selected_card.id}) at position {selected_position} in round {self.match_state.current_round}")
        logger.info(f"Player {self.user_id} used positions: {self.match_state.player1_used_cards if self.user_id == self.match_state.player1_id else self.match_state.player2_used_cards}")
        logger.info(f"Player {self.user_id} used card IDs: {self.match_state.player1_used_card_ids if self.user_id == self.match_state.player1_id else self.match_state.player2_used_card_ids}")

        # Handle the selection (similar to the old pick command logic)
        await interaction.response.defer(ephemeral=True)
        
        # Use the selected_card from team to ensure we have the correct card name
        selected_card_name = selected_card.name

        # Check if both players have selected
        if self.user_id == self.match_state.player1_id:
            # Player 1 selected - switch turn to player 2
            # CRITICAL: Clear any stale position from previous rounds
            self.match_state.last_player1_position = None
            
            # CRITICAL: Store P1's selection for when P2 picks (sets are unordered!)
            self.match_state.last_player1_position = selected_position
            self.match_state.current_turn = self.match_state.player2_id
            
            # CRITICAL: Save updated match state to Redis
            await self.cog._save_match_state(match_channel.id, self.match_state)
            
            # CRITICAL: Get fresh match state from Redis
            fresh_state = await self.cog._get_match_state(match_channel.id)
            if not fresh_state:
                logger.error("Failed to retrieve fresh match state after saving!")
                return

            # Confirm to player 1
            await interaction.followup.send(
                f"✅ You selected **{selected_card_name}** ({selected_position})!",
                ephemeral=True
            )
            
            # Use centralized turn announcement
            await self.cog._announce_turn(match_channel.id, fresh_state)
            
        else:
            # Player 2 selected, now play the round
            # Get player 1's selection from stored position
            player1_position = self.match_state.last_player1_position
            player2_position = selected_position
            
            if not player1_position:
                logger.error("CRITICAL BUG: Player 1 position not stored!")
                await interaction.followup.send(
                    "❌ Error: Player 1's selection was not found!",
                    ephemeral=True
                )
                return

            logger.info(f"Playing round with P1 position: {player1_position}, P2 position: {player2_position}")
            
            # Play round
            round_data = self.match_state.play_round(player1_position, player2_position)
            
            # DO NOT clear last_player1_position here - keep it so we can verify it was used correctly
            # It will be cleared when Player 1 makes their NEXT selection
            
            # CRITICAL: Save updated match state to Redis after round is played
            await self.cog._save_match_state(match_channel.id, self.match_state)
            
            # CRITICAL: Get fresh match state from Redis to ensure next dropdown is built with latest state
            fresh_state = await self.cog._get_match_state(match_channel.id)
            if not fresh_state:
                logger.error("Failed to retrieve fresh match state after saving!")
                return

            # Show round result PUBLICLY in channel
            player1 = await self.cog.bot.fetch_user(fresh_state.player1_id)
            player2 = await self.cog.bot.fetch_user(fresh_state.player2_id)
            embed = EmbedBuilder.match_round_embed(
                round_data, player1.name, player2.name
            )

            # Send confirmation to player 2 ephemerally
            await interaction.followup.send(
                f"✅ You selected **{selected_card_name}** ({selected_position})!",
                ephemeral=True
            )
            
            # Post round result publicly
            await match_channel.send(embed=embed)

            # Check if match is complete
            if fresh_state.is_complete():
                await self.cog._complete_match(interaction, fresh_state, match_channel)
            else:
                # Use centralized turn announcement
                await self.cog._announce_turn(match_channel.id, fresh_state)


class MatchCog(commands.Cog):
    """Match and betting commands"""
    
    def __init__(self, bot):
        self.bot = bot
        # Fallback in-memory storage if Redis unavailable
        self.active_matches = {}  # {channel_id: MatchState}
        self.last_dropdown_sent = {}  # {channel_id: {"round": int, "users_sent": set()}} - Track dropdowns sent per user per round

    async def _get_match_state(self, channel_id: int) -> Optional[MatchState]:
        """Get match state from Redis or fallback to memory"""
        # Try Redis first
        match_state = await redis_manager.get_match_state(channel_id)
        if match_state:
            return match_state
        
        # Fallback to in-memory
        return self.active_matches.get(channel_id)

    async def _save_match_state(self, channel_id: int, match_state: MatchState):
        """Save match state to Redis and memory"""
        # Save to Redis
        await redis_manager.save_match_state(channel_id, match_state)
        # Also keep in memory as fallback
        self.active_matches[channel_id] = match_state

    async def _delete_match_state(self, channel_id: int):
        """Delete match state from Redis and memory"""
        await redis_manager.delete_match_state(channel_id)
        if channel_id in self.active_matches:
            del self.active_matches[channel_id]

    async def _announce_turn(self, channel_id: int, match_state: MatchState):
        """Centralized turn announcement - ONLY place to send turn notifications"""
        next_id = match_state.current_turn
        channel = self.bot.get_channel(channel_id)
        if not channel:
            logger.error(f"Cannot announce turn: channel {channel_id} not found")
            return

        await channel.send(
            f"🔄 **Round {match_state.current_round}** - <@{next_id}>, it's your turn! Check your DMs!"
        )
        await self._send_player_pick_menu(channel_id, next_id)

    async def _send_player_pick_menu(self, channel_id: int, user_id: int):
        """Send a PRIVATE (DM) pick menu to the correct player ONLY"""
        try:
            # CRITICAL: Always get fresh state from Redis first
            match_state = await self._get_match_state(channel_id)
            if not match_state:
                logger.error(f"No match state found for channel {channel_id}")
                return

            # Validate user_id is one of the players
            if user_id != match_state.player1_id and user_id != match_state.player2_id:
                logger.error(f"Invalid user_id {user_id} passed to _send_player_pick_menu. Player1: {match_state.player1_id}, Player2: {match_state.player2_id}")
                return

            # Validate it's the correct user's turn - CRITICAL: Only send to current player
            if match_state.current_turn != user_id:
                logger.error(f"ERROR: Attempted to send pick menu to user {user_id} but current_turn is {match_state.current_turn}. Aborting to prevent showing wrong team!")
                return

            # Check if already sent using Redis (with fallback to memory)
            already_sent = await redis_manager.check_and_mark_dropdown_sent(
                channel_id, match_state.current_round, user_id
            )
            if already_sent:
                logger.warning(f"DUPLICATE DROPDOWN PREVENTED: Already sent dropdown for round {match_state.current_round} to user {user_id}. Skipping!")
                return

            logger.info(f"Sending PRIVATE DM pick menu to user {user_id} for round {match_state.current_round}")
            user = await self.bot.fetch_user(user_id)

            # Use get_available_cards to ensure consistency with dropdown building
            available_cards = match_state.get_available_cards(user_id)

            # Log for debugging
            logger.info(f"Sending PRIVATE DM to user {user_id} (name: {user.name}) for round {match_state.current_round}. Available cards: {len(available_cards)}")
            if user_id == match_state.player1_id:
                logger.info(f"Player 1 used positions: {match_state.player1_used_cards}, used card IDs: {match_state.player1_used_card_ids}")
                logger.info(f"Player 1 available positions: {list(available_cards.keys())}")
            else:
                logger.info(f"Player 2 used positions: {match_state.player2_used_cards}, used card IDs: {match_state.player2_used_card_ids}")
                logger.info(f"Player 2 available positions: {list(available_cards.keys())}")

            embed = discord.Embed(
                title=f"⚽ Your Turn - Round {match_state.current_round}!",
                description=f"Select a player from the dropdown below!\n\n"
                          f"🔒 **This is a private DM - your opponent cannot see this.**",
                color=discord.Color.blue()
            )

            # Add available players info with stats
            player_list = []
            for pos, card in list(available_cards.items())[:15]:  # Show more players
                player_list.append(f"**{card.name}** ({pos}) - {card.attack_stat} ATK / {card.defense_stat} DEF")
            
            if len(available_cards) > 15:
                player_list.append(f"\n... and {len(available_cards) - 15} more players available")

            embed.add_field(
                name=f"📋 Your Available Players ({len(available_cards)} remaining)",
                value="\n".join(player_list) if player_list else "No players available",
                inline=False
            )

            # Get the match channel
            match_channel = self.bot.get_channel(channel_id)
            
            # Set footer with channel name if available
            if match_channel and hasattr(match_channel, 'name'):
                embed.set_footer(text=f"Match in #{match_channel.name}")
            else:
                embed.set_footer(text="Match in progress")

            # Create view with validated user_id and channel reference
            view = PlayerSelectView(match_state, user_id, self, match_channel)

            # SEND VIA DM - TRUE PRIVACY
            try:
                await user.send(embed=embed, view=view)
                logger.info(f"Successfully sent DM pick menu to user {user_id}")
            except discord.Forbidden:
                # User has DMs disabled - fallback to ephemeral in channel
                logger.warning(f"User {user_id} has DMs disabled, sending ephemeral message instead")
                # For DM fallback, we need to handle this differently since we don't have interaction here
                # This will be handled when the user tries to use /pick command
                pass

        except Exception as e:
            logger.error(f"Error sending player pick menu: {e}", exc_info=True)

    async def _get_team_data(self, session: AsyncSession, user_id: int) -> tuple[Optional[Team], Dict]:
        """Get team and slots for a user"""
        result = await session.execute(
            select(Team).where(Team.user_id == user_id)
        )
        team = result.scalar_one_or_none()
        
        if not team or not team.formation:
            return None, {}

        # Get team slots - order by ID to ensure consistent results
        result = await session.execute(
            select(TeamSlot, Card)
            .join(Card, TeamSlot.card_id == Card.id)
            .where(TeamSlot.team_id == team.id)
            .order_by(TeamSlot.id)
        )
        slots = result.all()

        # Build team_slots dictionary, handling any duplicate positions
        # If there are duplicates, use the first one (by ID) and log a warning
        team_slots = {}
        seen_positions = set()
        
        for slot, card in slots:
            if slot.position in seen_positions:
                logger.warning(
                    f"Duplicate position '{slot.position}' for user {user_id} in team {team.id}. "
                    f"Position already has card {team_slots[slot.position].name}, "
                    f"skipping duplicate with card {card.name} (slot ID: {slot.id})"
                )
                continue
                
            team_slots[slot.position] = card
            seen_positions.add(slot.position)

        # Ensure we have 11 players
        if len(team_slots) < 11:
            logger.warning(f"Team {team.id} for user {user_id} has only {len(team_slots)} players, need 11")
            return team, {}
            
        return team, team_slots

    async def _update_leaderboard(self, session: AsyncSession, guild_id: int, user_id: int, won: bool, draw: bool):
        """Update leaderboard entry for a user"""
        result = await session.execute(
            select(Leaderboard)
            .where(Leaderboard.guild_id == guild_id)
            .where(Leaderboard.user_id == user_id)
        )
        lb_entry = result.scalar_one_or_none()
        
        if not lb_entry:
            lb_entry = Leaderboard(
                guild_id=guild_id,
                user_id=user_id,
                points=0,
                wins=0,
                draws=0,
                losses=0
            )
            session.add(lb_entry)

        # Update stats
        if won:
            lb_entry.wins += 1
            lb_entry.points += 3
        elif draw:
            lb_entry.draws += 1
            lb_entry.points += 1
        else:
            lb_entry.losses += 1

        await session.commit()

    @app_commands.command(name="match", description="Start a match against another user")
    @app_commands.describe(opponent="The user you want to challenge")
    async def start_match(self, interaction: discord.Interaction, opponent: discord.Member):
        """Start a match against another user"""
        # Defer immediately to avoid interaction timeout
        await interaction.response.defer(ephemeral=False)

        # Early validation
        if opponent.bot or opponent.id == interaction.user.id:
            await interaction.followup.send(
                "❌ You can't challenge bots or yourself!",
                ephemeral=True
            )
            return

        # Check if there's already an active match in this channel
        existing_match = await self._get_match_state(interaction.channel_id)
        if existing_match:
            await interaction.followup.send(
                "❌ There's already an active match in this channel!",
                ephemeral=True
            )
            return

        try:
            async with AsyncSessionLocal() as session:
                # Get both teams
                player1_team, player1_slots = await self._get_team_data(session, interaction.user.id)
                player2_team, player2_slots = await self._get_team_data(session, opponent.id)

                if not player1_team or not player1_slots:
                    await interaction.followup.send(
                        "❌ You need a complete team with 11 players to play!",
                        ephemeral=True
                    )
                    return

                if not player2_team or not player2_slots:
                    await interaction.followup.send(
                        f"❌ {opponent.mention} needs a complete team with 11 players to play!",
                        ephemeral=True
                    )
                    return

                # Create match state
                match_state = MatchState(
                    player1_id=interaction.user.id,
                    player2_id=opponent.id,
                    player1_team=player1_slots,
                    player2_team=player2_slots,
                    player1_formation=player1_team.formation,
                    player2_formation=player2_team.formation
                )

                # Store in Redis and active matches
                await self._save_match_state(interaction.channel_id, match_state)

                # Create active match record in database
                active_match = ActiveMatch(
                    guild_id=interaction.guild.id,
                    channel_id=interaction.channel_id,
                    player1_id=interaction.user.id,
                    player2_id=opponent.id,
                    current_round=1,
                    current_turn_player=interaction.user.id,
                    game_state={}
                )
                session.add(active_match)
                await session.commit()

                # Create PUBLIC match announcement (NO team info - that's private!)
                embed = discord.Embed(
                    title="⚽ Match Started!",
                    description=f"**{interaction.user.mention}** vs **{opponent.mention}**\n\n"
                              f"🎯 11 rounds of tactical football!",
                    color=discord.Color.blue()
                )
                embed.add_field(
                    name="How to Play",
                    value="• Each player selects 1 card per round\n"
                          "• Odd rounds: Player 1 attacks\n"
                          "• Even rounds: Player 2 attacks\n"
                          "• Attack stat vs Defense stat determines winner\n"
                          "• Winner of most rounds wins the match!",
                    inline=False
                )
                embed.add_field(
                    name="🔒 Privacy",
                    value="Team selections are private. Each player receives their options via ephemeral messages.",
                    inline=False
                )

                await interaction.followup.send(embed=embed)

                # Use centralized turn announcement for Round 1
                await self._announce_turn(interaction.channel_id, match_state)

        except Exception as e:
            logger.error(f"Error in start_match: {e}", exc_info=True)
            await interaction.followup.send(
                "❌ An error occurred while starting the match.",
                ephemeral=True
            )

    @app_commands.command(name="pick", description="Pick a player for the current match round (shows dropdown menu)")
    async def select_player(self, interaction: discord.Interaction):
        """Show player pick dropdown menu"""
        await interaction.response.defer(ephemeral=True)

        # Check if there's an active match
        match_state = await self._get_match_state(interaction.channel_id)
        if not match_state:
            await interaction.followup.send(
                "❌ No active match in this channel!",
                ephemeral=True
            )
            return

        # Check if it's this user's turn
        if match_state.current_turn != interaction.user.id:
            await interaction.followup.send(
                "❌ It's not your turn!",
                ephemeral=True
            )
            return

        # Send pick menu (duplicate prevention handled inside)
        await self._send_player_pick_menu(interaction.channel_id, interaction.user.id)
        await interaction.followup.send(
            "✅ Check your DMs for the pick menu!",
            ephemeral=True
        )

    async def _complete_match(self, interaction: discord.Interaction, match_state: MatchState, match_channel=None):
        """Complete a match and process results"""
        # Note: This is a helper function called from within a command that already deferred
        # Do not defer here as the interaction was already handled
        
        try:
            async with AsyncSessionLocal() as session:
                # Get users
                player1 = await self.bot.fetch_user(match_state.player1_id)
                player2 = await self.bot.fetch_user(match_state.player2_id)

                # Create match record
                winner_id = match_state.get_winner()
                
                # CRITICAL: Log final scores for debugging
                logger.info(f"Match complete! Player 1 ({player1.name}): {match_state.player1_score}, Player 2 ({player2.name}): {match_state.player2_score}")
                logger.info(f"Winner ID: {winner_id}, Player 1 ID: {match_state.player1_id}, Player 2 ID: {match_state.player2_id}")
                logger.info(f"Round history: {len(match_state.round_history)} rounds")
                
                # Log round-by-round details with card IDs for verification
                for i, round_data in enumerate(match_state.round_history, 1):
                    p1_card = round_data.get('player1_card', 'N/A')
                    p1_card_id = round_data.get('player1_card_id', 'N/A')
                    p2_card = round_data.get('player2_card', 'N/A')
                    p2_card_id = round_data.get('player2_card_id', 'N/A')
                    winner_id_round = round_data.get('winner_id', 'N/A')
                    logger.info(f"Round {i}: Score={round_data.get('score', 'N/A')}, P1={p1_card} (ID: {p1_card_id}), P2={p2_card} (ID: {p2_card_id}), Winner={winner_id_round}")
                
                # Log round winners summary
                if hasattr(match_state, 'round_winners'):
                    p1_wins = sum(1 for w in match_state.round_winners if w == match_state.player1_id)
                    p2_wins = sum(1 for w in match_state.round_winners if w == match_state.player2_id)
                    draws = sum(1 for w in match_state.round_winners if w is None)
                    logger.info(f"Round winners summary: Player 1 wins={p1_wins}, Player 2 wins={p2_wins}, Draws={draws}")

                # Get guild_id from match_channel if available, otherwise from interaction
                guild_id = match_channel.guild.id if match_channel else interaction.guild.id

                match_record = Match(
                    guild_id=guild_id,
                    player1_id=match_state.player1_id,
                    player2_id=match_state.player2_id,
                    player1_score=match_state.player1_score,
                    player2_score=match_state.player2_score,
                    winner_id=winner_id,
                    match_details=json.dumps([r for r in match_state.round_history]),
                    completed_at=discord.utils.utcnow()
                )
                session.add(match_record)

                # Update user stats
                result = await session.execute(
                    select(User).where(User.id.in_([match_state.player1_id, match_state.player2_id]))
                )
                users = result.scalars().all()
                
                for user in users:
                    user.total_games += 1
                    if winner_id == user.id:
                        user.total_wins += 1
                    elif winner_id is None:
                        user.total_draws += 1
                    else:
                        user.total_losses += 1

                # Update leaderboard
                await self._update_leaderboard(
                    session, guild_id, match_state.player1_id,
                    won=(winner_id == match_state.player1_id),
                    draw=(winner_id is None)
                )
                await self._update_leaderboard(
                    session, guild_id, match_state.player2_id,
                    won=(winner_id == match_state.player2_id),
                    draw=(winner_id is None)
                )

                # Remove active match - handle multiple matches if they exist
                channel_id = match_channel.id if match_channel else interaction.channel_id
                result = await session.execute(
                    select(ActiveMatch).where(ActiveMatch.channel_id == channel_id)
                )
                active_matches = result.scalars().all()
                for active in active_matches:
                    await session.delete(active)

                # Process any active bets BEFORE committing
                await self._process_bets(session, guild_id, match_state.player1_id, match_state.player2_id, winner_id)
                
                await session.commit()

                # Show match complete embed
                embed = EmbedBuilder.match_complete_embed(match_state, player1.name, player2.name)
                
                # Send to match channel if available, otherwise to interaction channel
                target_channel = match_channel if match_channel else interaction.channel
                await target_channel.send(embed=embed)

                # Remove from Redis and active matches
                channel_id = match_channel.id if match_channel else interaction.channel_id
                await self._delete_match_state(channel_id)

        except Exception as e:
            logger.error(f"Error in _complete_match: {e}", exc_info=True)
            # Try to send error message
            try:
                await interaction.channel.send(
                    "❌ An error occurred while completing the match.",
                )
            except:
                pass

    async def _process_bets(self, session: AsyncSession, guild_id: int, player1_id: int, player2_id: int, winner_id: Optional[int]):
        """Process bets for completed match - FIXED VERSION"""
        logger.info(f"_process_bets: Starting for players {player1_id} vs {player2_id}, guild {guild_id}, winner: {winner_id}")
        
        try:
            # Find all active bets between these two players
            result = await session.execute(
                select(Bet)
                .where(Bet.guild_id == guild_id)
                .where(Bet.accepted == True)
                .where(Bet.completed == False)
                .where(
                    ((Bet.creator_id == player1_id) & (Bet.challenged_id == player2_id)) |
                    ((Bet.creator_id == player2_id) & (Bet.challenged_id == player1_id))
                )
            )
            bets = result.scalars().all()
            
            logger.info(f"_process_bets: Found {len(bets)} bets for players {player1_id} vs {player2_id}")

            for bet in bets:
                logger.info(f"Processing bet {bet.id}: Creator={bet.creator_id}, Challenged={bet.challenged_id}, "
                          f"Creator Cards={bet.creator_cards}, Challenged Cards={bet.challenged_cards}")

                if winner_id is None:
                    # Draw - return cards (no transfer)
                    logger.info(f"Bet {bet.id}: Draw occurred, marking completed without transfer")
                    bet.completed = True
                    continue

                # Determine winner and loser
                if winner_id == bet.creator_id:
                    # Creator won - they get challenged's cards
                    winner_id_bet = bet.creator_id
                    loser_id = bet.challenged_id
                    cards_to_transfer = bet.challenged_cards or []
                    logger.info(f"Bet {bet.id}: Creator {winner_id_bet} won, transferring {len(cards_to_transfer)} cards from challenged {loser_id}")
                else:
                    # Challenged won - they get creator's cards
                    winner_id_bet = bet.challenged_id
                    loser_id = bet.creator_id
                    cards_to_transfer = bet.creator_cards or []
                    logger.info(f"Bet {bet.id}: Challenged {winner_id_bet} won, transferring {len(cards_to_transfer)} cards from creator {loser_id}")

                # Transfer each card from loser to winner
                transferred_cards = []
                failed_transfers = []
                
                for card_id in cards_to_transfer:
                    try:
                        # Ensure card_id is integer
                        card_id = int(card_id)
                        
                        # Check if card exists in loser's collection
                        result = await session.execute(
                            select(Collection)
                            .where(Collection.user_id == loser_id)
                            .where(Collection.card_id == card_id)
                        )
                        loser_collection = result.scalar_one_or_none()
                        
                        if loser_collection:
                            # Remove from loser
                            await session.delete(loser_collection)
                            logger.info(f"Bet {bet.id}: Removed card {card_id} from loser {loser_id}")
                        else:
                            logger.warning(f"Bet {bet.id}: Card {card_id} not found in loser {loser_id}'s collection, but will still add to winner")

                        # Add to winner (regardless of whether it was in loser's collection)
                        # Check if winner already has this card
                        result = await session.execute(
                            select(Collection)
                            .where(Collection.user_id == winner_id_bet)
                            .where(Collection.card_id == card_id)
                        )
                        existing_collection = result.scalar_one_or_none()
                        
                        if not existing_collection:
                            new_collection = Collection(
                                user_id=winner_id_bet,
                                card_id=card_id
                            )
                            session.add(new_collection)
                            transferred_cards.append(card_id)
                            logger.info(f"Bet {bet.id}: Added card {card_id} to winner {winner_id_bet}")
                        else:
                            logger.warning(f"Bet {bet.id}: Winner {winner_id_bet} already has card {card_id}, skipping duplicate")
                            failed_transfers.append(card_id)
                            
                    except Exception as card_error:
                        logger.error(f"Bet {bet.id}: Error transferring card {card_id}: {card_error}")
                        failed_transfers.append(card_id)

                # Update bet status
                bet.completed = True
                bet.winner_id = winner_id
                
                logger.info(f"Bet {bet.id}: Completed. Transferred {len(transferred_cards)} cards successfully, "
                          f"{len(failed_transfers)} failed transfers")

                # Send notification about the bet result
                try:
                    winner_user = await self.bot.fetch_user(winner_id_bet)
                    loser_user = await self.bot.fetch_user(loser_id)
                    
                    # Get card names for notification
                    card_names = []
                    for card_id in transferred_cards:
                        result = await session.execute(
                            select(Card).where(Card.id == card_id)
                        )
                        card = result.scalar_one_or_none()
                        if card:
                            card_names.append(card.name)
                    
                    if card_names:
                        # Find the match channel to send notification
                        channel = self.bot.get_channel(guild_id)  # This might need adjustment
                        if channel and hasattr(channel, 'send'):
                            embed = discord.Embed(
                                title="🎲 Bet Result!",
                                description=f"**{winner_user.name}** won the bet against **{loser_user.name}**!",
                                color=discord.Color.gold()
                            )
                            embed.add_field(
                                name="🏆 Cards Transferred",
                                value="\n".join([f"• {name}" for name in card_names[:10]]),
                                inline=False
                            )
                            if len(card_names) > 10:
                                embed.add_field(
                                    name="And more...",
                                    value=f"Plus {len(card_names) - 10} more cards!",
                                    inline=False
                                )
                            
                            await channel.send(embed=embed)
                except Exception as notify_error:
                    logger.error(f"Error sending bet notification: {notify_error}")

            logger.info(f"_process_bets: Completed processing {len(bets)} bets")
            
        except Exception as e:
            logger.error(f"Error in _process_bets: {e}", exc_info=True)
            raise  # Re-raise to handle in caller

    @app_commands.command(name="bet", description="Bet cards against another user")
    @app_commands.describe(
        opponent="The user you want to bet against",
        card_name="Name of card to bet (use command multiple times for multiple cards)"
    )
    async def create_bet(self, interaction: discord.Interaction, opponent: discord.Member, card_name: str):
        """Create or add to a bet"""
        # Defer immediately to avoid interaction timeout
        await interaction.response.defer(ephemeral=False)

        # Early validation
        if opponent.bot or opponent.id == interaction.user.id:
            await interaction.followup.send(
                "❌ You can't bet against bots or yourself!",
                ephemeral=True
            )
            return

        try:
            async with AsyncSessionLocal() as session:
                # Find card in collection
                result = await session.execute(
                    select(Card, Collection)
                    .join(Collection, Card.id == Collection.card_id)
                    .where(Collection.user_id == interaction.user.id)
                    .where(Card.name.ilike(f"%{card_name}%"))
                )
                card_data = result.first()
                
                if not card_data:
                    await interaction.followup.send(
                        f"❌ You don't have a card matching '{card_name}'!",
                        ephemeral=True
                    )
                    return
                    
                card, _ = card_data

                # Check for existing bet where user is creator
                result = await session.execute(
                    select(Bet)
                    .where(Bet.guild_id == interaction.guild.id)
                    .where(Bet.creator_id == interaction.user.id)
                    .where(Bet.challenged_id == opponent.id)
                    .where(Bet.accepted == False)
                )
                existing_bet_as_creator = result.scalar_one_or_none()

                # Check for existing bet where user is challenged (accepting a bet)
                result = await session.execute(
                    select(Bet)
                    .where(Bet.guild_id == interaction.guild.id)
                    .where(Bet.creator_id == opponent.id)
                    .where(Bet.challenged_id == interaction.user.id)
                    .where(Bet.accepted == False)
                )
                existing_bet_as_challenged = result.scalar_one_or_none()

                if existing_bet_as_creator:
                    # User is creator, adding more cards to their bet
                    if len(existing_bet_as_creator.creator_cards) >= 3:
                        await interaction.followup.send(
                            "❌ You can only bet up to 3 cards!",
                            ephemeral=True
                        )
                        return

                    existing_bet_as_creator.creator_cards.append(card.id)
                    await session.commit()
                    
                    await interaction.followup.send(
                        f"✅ Added **{card.name}** to your bet against {opponent.mention}!",
                        ephemeral=True
                    )

                elif existing_bet_as_challenged:
                    # User is challenged, accepting the bet by adding their cards
                    if len(existing_bet_as_challenged.challenged_cards) >= 3:
                        await interaction.followup.send(
                            "❌ You can only bet up to 3 cards!",
                            ephemeral=True
                        )
                        return

                    existing_bet_as_challenged.challenged_cards.append(card.id)

                    # Check if both players have matched number of cards
                    if len(existing_bet_as_challenged.challenged_cards) == len(existing_bet_as_challenged.creator_cards):
                        existing_bet_as_challenged.accepted = True
                        await interaction.followup.send(
                            f"✅ Bet accepted! You matched with **{card.name}**.\n"
                            f"🎯 The bet is now active! Winner takes all cards.",
                            ephemeral=False
                        )
                    else:
                        await interaction.followup.send(
                            f"✅ Added **{card.name}** to your bet!\n"
                            f"Match {len(existing_bet_as_challenged.creator_cards)} cards to accept the bet.",
                            ephemeral=False
                        )
                    
                    await session.commit()

                else:
                    # Create new bet
                    new_bet = Bet(
                        guild_id=interaction.guild.id,
                        creator_id=interaction.user.id,
                        challenged_id=opponent.id,
                        creator_cards=[card.id],
                        challenged_cards=[]
                    )
                    session.add(new_bet)
                    await session.commit()
                    
                    embed = discord.Embed(
                        title="🎲 New Bet Created!",
                        description=f"{interaction.user.mention} has challenged {opponent.mention} to a bet!",
                        color=discord.Color.gold()
                    )
                    embed.add_field(name="Wagered Card", value=f"**{card.name}**", inline=False)
                    embed.add_field(
                        name="To Accept",
                        value=f"{opponent.mention}, use `/bet {interaction.user.mention} <card_name>` to match the bet!",
                        inline=False
                    )
                    
                    await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Error in create_bet: {e}", exc_info=True)
            await interaction.followup.send(
                "❌ An error occurred while creating the bet.",
                ephemeral=True
            )

    @app_commands.command(name="leaderboard", description="View the server leaderboard")
    async def view_leaderboard(self, interaction: discord.Interaction):
        """View the server leaderboard"""
        await interaction.response.defer(ephemeral=False)
        
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Leaderboard, User)
                    .join(User, Leaderboard.user_id == User.id)
                    .where(Leaderboard.guild_id == interaction.guild.id)
                    .order_by(Leaderboard.points.desc())
                )
                entries = result.all()
                
                embed = EmbedBuilder.leaderboard_embed(
                    interaction.guild.name,
                    [(user, lb) for lb, user in entries]
                )
                
                await interaction.followup.send(embed=embed)
                
        except Exception as e:
            logger.error(f"Error in view_leaderboard: {e}", exc_info=True)
            await interaction.followup.send(
                "❌ An error occurred while fetching the leaderboard.",
                ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(MatchCog(bot))
