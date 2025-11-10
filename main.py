import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import random
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import os
import logging
import sys

# Set up logging to stdout only
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

logging.basicConfig(
    level=logging.INFO,
    handlers=[handler]
)
logger = logging.getLogger('giftbot')

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# File to store persistent data - read from environment variable or use default
DATA_FILE = os.getenv("DATA_FILE_PATH", "game_state.json")


class GameState:
    """Represents the state of a number guessing game for a channel"""
    
    def __init__(self, channel_id: int):
        self.channel_id = channel_id
        self.active = False
        self.paused = False
        self.number = 0
        self.min_number = 0
        self.max_number = 500
        self.timeout_minutes = 10
        self.end_time = None
        self.closest_offset = None
        self.winning_user_id = None
        self.keys: List[Dict[str, str]] = []  # List of {game_name, key}
        self.current_round = 0
        self.total_rounds = 0
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            "channel_id": self.channel_id,
            "active": self.active,
            "paused": self.paused,
            "number": self.number,
            "min_number": self.min_number,
            "max_number": self.max_number,
            "timeout_minutes": self.timeout_minutes,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "closest_offset": self.closest_offset,
            "winning_user_id": self.winning_user_id,
            "keys": self.keys,
            "current_round": self.current_round,
            "total_rounds": self.total_rounds
        }
    
    @staticmethod
    def from_dict(data: dict):
        """Create GameState from dictionary"""
        state = GameState(data["channel_id"])
        state.active = data.get("active", False)
        state.paused = data.get("paused", False)
        state.number = data.get("number", 0)
        state.min_number = data.get("min_number", 0)
        state.max_number = data.get("max_number", 500)
        state.timeout_minutes = data.get("timeout_minutes", 10)
        
        end_time_str = data.get("end_time")
        state.end_time = datetime.fromisoformat(end_time_str) if end_time_str else None
        
        state.closest_offset = data.get("closest_offset")
        state.winning_user_id = data.get("winning_user_id")
        state.keys = data.get("keys", [])
        state.current_round = data.get("current_round", 0)
        state.total_rounds = data.get("total_rounds", 0)
        
        return state


class NumberGuessBot:
    """Main bot class managing number guessing games"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.games: Dict[int, GameState] = {}  # channel_id -> GameState
        self.load_state()
    
    def load_state(self):
        """Load game states from JSON file"""
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, 'r') as f:
                    data = json.load(f)
                    for channel_id_str, game_data in data.items():
                        channel_id = int(channel_id_str)
                        self.games[channel_id] = GameState.from_dict(game_data)
                logger.info(f"Loaded {len(self.games)} game states from {DATA_FILE}")
            except Exception as e:
                logger.error(f"Error loading state: {e}", exc_info=True)
        else:
            logger.info(f"No existing state file found, starting fresh")
    
    def save_state(self):
        """Save game states to JSON file"""
        try:
            data = {str(channel_id): state.to_dict() 
                   for channel_id, state in self.games.items()}
            with open(DATA_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving state: {e}", exc_info=True)
    
    def get_or_create_game(self, channel_id: int) -> GameState:
        """Get existing game state or create new one for channel"""
        if channel_id not in self.games:
            self.games[channel_id] = GameState(channel_id)
            self.save_state()
        return self.games[channel_id]
    
    async def process_message(self, message: discord.Message):
        """Process a message for number guessing"""
        if message.author.bot:
            return
        
        channel_id = message.channel.id
        if channel_id not in self.games:
            return
        
        game = self.games[channel_id]
        
        if not game.active or game.paused:
            return
        
        # Check if game has timed out
        if game.end_time and datetime.now() >= game.end_time:
            await self.finalize_round(message.channel, game)
            return
        
        # Extract numbers from message
        number_str = ''.join(c for c in message.content if c.isdigit())
        
        if not number_str:
            return
        
        try:
            guessed_number = int(number_str)
        except ValueError:
            return
        
        # Check if number is in valid range
        if guessed_number < game.min_number or guessed_number > game.max_number:
            return
        
        # Calculate offset
        offset = abs(game.number - guessed_number)
        
        # Check if this is a better guess
        if game.closest_offset is None or offset < game.closest_offset:
            logger.info(f"Game progressed: {game.closest_offset} -> {offset}, by {message.author.name} ({message.author.id}) in channel {channel_id}")
            game.closest_offset = offset
            game.winning_user_id = message.author.id
            self.save_state()
            
            # Check if exact match
            if offset == 0:
                await self.finalize_round(message.channel, game)
    
    async def finalize_round(self, channel: discord.TextChannel, game: GameState):
        """Finalize the current round of the game"""
        game.active = False
        
        if game.winning_user_id:
            # Announce winner
            if game.closest_offset == 0:
                accuracy_msg = f"The hidden number was exactly **{game.number}**."
            else:
                accuracy_msg = f"The hidden number was **{game.number}**. The user was **{game.closest_offset}** off!"
            
            await channel.send(
                f"🎉 User <@{game.winning_user_id}> won round {game.current_round}/{game.total_rounds}! {accuracy_msg}"
            )
            
            # Send reward key via DM
            if game.current_round <= len(game.keys):
                key_info = game.keys[game.current_round - 1]
                try:
                    user = await self.bot.fetch_user(game.winning_user_id)
                    dm_channel = await user.create_dm()
                    await dm_channel.send(
                        f"🎁 **Congratulations!** You won round {game.current_round}!\n\n"
                        f"**Game:** {key_info['game_name']}\n"
                        f"**Key:** `{key_info['key']}`"
                    )
                except discord.Forbidden:
                    await channel.send(f"❌ Failed to send DM to <@{game.winning_user_id}>. Please enable DMs.")
                    logger.warning(f"Failed to send DM to user {game.winning_user_id}: DMs disabled")
                except Exception as e:
                    logger.error(f"Error sending DM to user {game.winning_user_id}: {e}", exc_info=True)
                    await channel.send(f"❌ Failed to send message to <@{game.winning_user_id}>")
        else:
            await channel.send(f"⏰ Round {game.current_round}/{game.total_rounds} ended with no winner!")
        
        # Check if there are more rounds
        if game.current_round < game.total_rounds:
            # Automatically start the next round
            self.start_round(game)
            
            # Get the game name for this round
            game_name = game.keys[game.current_round - 1]['game_name'] if game.current_round <= len(game.keys) else "Unknown"
            
            await channel.send(
                f"▶️ **Round {game.current_round}/{game.total_rounds} Starting!**\n"
                f"🎮 Game: **{game_name}**\n"
                f"🎲 Guess a number between **{game.min_number}** and **{game.max_number}**!\n"
                f"⏱️ Time limit: **{game.timeout_minutes}** minutes\n"
                f"💡 Just type any message with a number in this channel!"
            )
        else:
            # Game completely finished
            await channel.send("🏁 All rounds completed! Game over.")
            game.paused = False
            game.current_round = 0
            game.total_rounds = 0
            game.keys = []
        
        self.save_state()
    
    def start_round(self, game: GameState):
        """Start a new round"""
        game.current_round += 1
        game.number = random.randint(game.min_number, game.max_number)
        game.closest_offset = None
        game.winning_user_id = None
        game.end_time = datetime.now() + timedelta(minutes=game.timeout_minutes)
        game.active = True
        game.paused = False
        self.save_state()
        
        logger.info(f"Round {game.current_round}/{game.total_rounds} started in channel {game.channel_id}. Number is {game.number}")


# Create bot instance
number_guess_bot = NumberGuessBot(bot)

# Create command group
game_group = app_commands.Group(name="game", description="Number guessing game commands")


@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    
    # Add the game group to the command tree
    bot.tree.add_command(game_group)
    
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}", exc_info=True)
    
    # Start background task to check for timeouts
    check_timeouts.start()


@bot.event
async def on_message(message: discord.Message):
    """Handle incoming messages"""
    await number_guess_bot.process_message(message)
    await bot.process_commands(message)


@tasks.loop(seconds=5)
async def check_timeouts():
    """Background task to check for game timeouts"""
    for channel_id, game in list(number_guess_bot.games.items()):
        if game.active and not game.paused and game.end_time:
            if datetime.now() >= game.end_time:
                try:
                    channel = bot.get_channel(channel_id)
                    if channel:
                        await number_guess_bot.finalize_round(channel, game)
                except Exception as e:
                    logger.error(f"Error checking timeout for channel {channel_id}: {e}", exc_info=True)


@game_group.command(name="init", description="Initialize game settings for this channel")
@app_commands.describe(
    min_number="Minimum number in the range (default: 0)",
    max_number="Maximum number in the range (default: 500)",
    timeout_minutes="Minutes per round (default: 10)"
)
@app_commands.checks.has_permissions(manage_messages=True)
async def game_init(
    interaction: discord.Interaction,
    min_number: int = 0,
    max_number: int = 500,
    timeout_minutes: int = 10
):
    """Initialize game settings"""
    if min_number < 0 or max_number < min_number:
        await interaction.response.send_message("❌ Invalid number range!", ephemeral=True)
        return
    
    if timeout_minutes < 1 or timeout_minutes > 60:
        await interaction.response.send_message("❌ Timeout must be between 1 and 60 minutes!", ephemeral=True)
        return
    
    game = number_guess_bot.get_or_create_game(interaction.channel_id)
    
    if game.active:
        await interaction.response.send_message("❌ Cannot change settings while a game is active!", ephemeral=True)
        return
    
    game.min_number = min_number
    game.max_number = max_number
    game.timeout_minutes = timeout_minutes
    game.active = False
    game.paused = False
    game.winning_user_id = None
    game.closest_offset = 0
    game.current_round = 0
    game.total_rounds = 0
    number_guess_bot.save_state()
    
    await interaction.response.send_message(
        f"✅ Game settings updated!\n"
        f"📊 Number range: **{min_number}** to **{max_number}**\n"
        f"⏱️ Timeout: **{timeout_minutes}** minutes per round",
        ephemeral=True
    )

@game_group.command(name="addkeymulti", description="Add multiple game keys for rounds")
@app_commands.describe(
    file="File that contains game keys in the format 'Game Name Key', one per line. Line will be split on the last space."
)
@app_commands.checks.has_permissions(manage_messages=True)
async def game_addkeymulti(
    interaction: discord.Interaction,
    file: discord.Attachment
):
    """Add multiple game keys from a file"""
    game = number_guess_bot.get_or_create_game(interaction.channel_id)
    
    try:
        file_content = await file.read()
        lines = file_content.decode('utf-8').splitlines()
        prettyprint = []
        
        added_count = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Split on the last space to separate game name and key
            if ' ' not in line:
                continue
            game_name, key = line.rsplit(' ', 1)
            game.keys.append({"game_name": game_name.strip(), "key": key.strip()})
            added_count += 1
            prettyprint.append(f"- {game_name.strip()}")
        
        if game.active:
            game.total_rounds += added_count

        number_guess_bot.save_state()
        
        await interaction.response.send_message(
            f"✅ Added {added_count} keys! Total keys: **{len(game.keys)}**\n{'\n'.join(prettyprint)}",
            ephemeral=True
        )

    except Exception as e:
        logger.error(f"Error adding multiple keys: {e}", exc_info=True)
        await interaction.response.send_message(
            "❌ Failed to add keys from the file. Please ensure it is formatted correctly.",
            ephemeral=True
        )

@game_group.command(name="addkey", description="Add a game key for a round")
@app_commands.describe(
    game_name="Name of the game",
    key="The game key/code"
)
@app_commands.checks.has_permissions(manage_messages=True)
async def game_addkey(
    interaction: discord.Interaction,
    game_name: str,
    key: str
):
    """Add a game key"""
    game = number_guess_bot.get_or_create_game(interaction.channel_id)
    
    if game.active:
        game.total_rounds += 1
    
    game.keys.append({"game_name": game_name, "key": key})
    number_guess_bot.save_state()
    
    await interaction.response.send_message(
        f"✅ Added key for **{game_name}**! Total keys: **{len(game.keys)}**",
        ephemeral=True
    )


@game_group.command(name="listkeys", description="List all added keys (admin only)")
@app_commands.checks.has_permissions(manage_messages=True)
async def game_listkeys(interaction: discord.Interaction):
    """List all keys (for debugging/admin)"""
    game = number_guess_bot.get_or_create_game(interaction.channel_id)
    
    if not game.keys:
        await interaction.response.send_message("📭 No keys added yet!", ephemeral=True)
        return
    
    key_list = "\n".join([f"{i+1}. **{k['game_name']}**: `{k['key']}`" 
                          for i, k in enumerate(game.keys)])
    
    await interaction.response.send_message(
        f"🔑 **Game Keys ({len(game.keys)} total):**\n{key_list}",
        ephemeral=True
    )


@game_group.command(name="clearkeys", description="Clear all game keys")
@app_commands.checks.has_permissions(manage_messages=True)
async def game_clearkeys(interaction: discord.Interaction):
    """Clear all keys"""
    game = number_guess_bot.get_or_create_game(interaction.channel_id)
    
    if game.active:
        await interaction.response.send_message("❌ Cannot clear keys while a game is active!", ephemeral=True)
        return
    
    count = len(game.keys)
    game.keys = []
    game.current_round = 0
    game.total_rounds = 0
    number_guess_bot.save_state()
    
    await interaction.response.send_message(f"🗑️ Cleared {count} key(s)!", ephemeral=True)


@game_group.command(name="start", description="Start or resume the number guessing game")
@app_commands.checks.has_permissions(manage_messages=True)
async def game_start(interaction: discord.Interaction):
    """Start or resume the game"""
    game = number_guess_bot.get_or_create_game(interaction.channel_id)
    
    if not game.keys:
        await interaction.response.send_message("❌ No keys added! Use `/game_addkey` first.", ephemeral=True)
        return
    
    # If game is paused, resume it
    if game.paused:
        # Resume the current round (don't increment)
        game.number = random.randint(game.min_number, game.max_number)
        game.closest_offset = None
        game.winning_user_id = None
        game.end_time = datetime.now() + timedelta(minutes=game.timeout_minutes)
        game.active = True
        game.paused = False
        number_guess_bot.save_state()
        
        logger.info(f"Round {game.current_round}/{game.total_rounds} resumed in channel {interaction.channel_id}. Number is {game.number}")
        
        # Get the game name for this round
        game_name = game.keys[game.current_round - 1]['game_name'] if game.current_round <= len(game.keys) else "Unknown"
        
        await interaction.response.send_message(
            f"▶️ **Round {game.current_round}/{game.total_rounds} Resumed!**\n"
            f"🎮 Game: **{game_name}**\n"
            f"🎲 Guess a number between **{game.min_number}** and **{game.max_number}**!\n"
            f"⏱️ Time limit: **{game.timeout_minutes}** minutes\n"
            f"💡 Just type any message with a number in this channel!"
        )
        return
    
    # If game is already active
    if game.active:
        await interaction.response.send_message("❌ A game is already in progress!", ephemeral=True)
        return
    
    # Start new game
    game.total_rounds = len(game.keys)
    game.current_round = 0
    number_guess_bot.start_round(game)
    
    # Get the game name for round 1
    game_name = game.keys[0]['game_name'] if len(game.keys) > 0 else "Unknown"
    
    await interaction.response.send_message(
        f"🎮 **Number Guessing Game Started!**\n"
        f"🏆 Total rounds: **{game.total_rounds}**\n\n"
        f"▶️ **Round 1/{game.total_rounds}**\n"
        f"🎮 Game: **{game_name}**\n"
        f"🎲 Guess a number between **{game.min_number}** and **{game.max_number}**!\n"
        f"⏱️ Time limit: **{game.timeout_minutes}** minutes per round\n"
        f"💡 Just type any message with a number in this channel!"
    )


@game_group.command(name="pause", description="Pause the game between rounds")
@app_commands.checks.has_permissions(manage_messages=True)
async def game_pause(interaction: discord.Interaction):
    """Pause the game"""
    game = number_guess_bot.get_or_create_game(interaction.channel_id)
    
    if not game.active and not game.paused:
        await interaction.response.send_message("❌ No game is currently active!", ephemeral=True)
        return
    
    if game.paused:
        await interaction.response.send_message("❌ Game is already paused!", ephemeral=True)
        return
    
    # Immediately pause the current round
    game.active = False
    game.paused = True
    number_guess_bot.save_state()
    
    await interaction.response.send_message(
        f"⏸️ **Game Paused!**\n"
        f"Current round ({game.current_round}/{game.total_rounds}) has been paused.\n"
        f"Use `/game start` to resume from round {game.current_round}."
    )


@game_group.command(name="stop", description="Force stop the current game")
@app_commands.checks.has_permissions(manage_messages=True)
async def game_stop(interaction: discord.Interaction):
    """Force stop the game"""
    game = number_guess_bot.get_or_create_game(interaction.channel_id)
    
    if not game.active and not game.paused:
        await interaction.response.send_message("❌ No game is currently active!", ephemeral=True)
        return
    
    game.active = False
    game.paused = False
    game.current_round = 0
    game.total_rounds = 0
    number_guess_bot.save_state()
    
    await interaction.response.send_message("🛑 Game stopped!")


@game_group.command(name="status", description="Check the current game status")
@app_commands.checks.has_permissions(manage_messages=True)
async def game_status(interaction: discord.Interaction):
    """Check game status"""
    game = number_guess_bot.get_or_create_game(interaction.channel_id)
    
    if not game.active and not game.paused:
        await interaction.response.send_message(
            f"📊 **Game Status: Inactive**\n"
            f"Keys loaded: **{len(game.keys)}**\n"
            f"Settings: **{game.min_number}** to **{game.max_number}**, **{game.timeout_minutes}** min timeout"
        )
        return
    
    status = "⏸️ Paused" if game.paused else "▶️ Active"
    time_left = ""
    
    if game.active and game.end_time:
        remaining = game.end_time - datetime.now()
        if remaining.total_seconds() > 0:
            minutes = int(remaining.total_seconds() // 60)
            seconds = int(remaining.total_seconds() % 60)
            time_left = f"\n⏱️ Time left: **{minutes}m {seconds}s**"
    
    await interaction.response.send_message(
        f"📊 **Game Status: {status}**\n"
        f"🏆 Round: **{game.current_round}/{game.total_rounds}**\n"
        f"🎲 Range: **{game.min_number}** to **{game.max_number}**{time_left}"
    )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Handle errors from slash commands"""
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command! You need the **Manage Messages** permission.",
            ephemeral=True
        )
    else:
        # Log other errors
        logger.error(f"Command error in {interaction.command.name if interaction.command else 'unknown'}: {error}", exc_info=True)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"❌ An error occurred: {str(error)}",
                ephemeral=True
            )


# Run the bot
if __name__ == "__main__":
    import sys
    
    # Read bot token from environment variable
    TOKEN = os.getenv("BOT_TOKEN")
    
    if not TOKEN:
        # Fall back to command line argument if environment variable not set
        if len(sys.argv) < 2:
            logger.error("No bot token provided!")
            logger.error("Please set the BOT_TOKEN environment variable or pass the token as a command line argument.")
            logger.error("Usage: python main.py <YOUR_BOT_TOKEN>")
            sys.exit(1)
        TOKEN = sys.argv[1]
    
    logger.info(f"Using data file: {DATA_FILE}")
    
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.critical(f"Failed to start bot: {e}", exc_info=True)
        sys.exit(1)
