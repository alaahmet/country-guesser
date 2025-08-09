from flask import Flask
from threading import Thread
from discord.ext import tasks, commands
from itertools import cycle
import discord
import requests # Import the requests library
import g # Import the g.py file
import os # Import the os library
from dotenv import load_dotenv # Import dotenv

load_dotenv() # Load environment variables from .env file

app = Flask('')


@app.route('/')
def main():
  return "Your Bot Is Ready"


def run():
  app.run(host="0.0.0.0", port=8000)


def keep_alive():
  server = Thread(target=run)
  server.start()


TOKEN = os.getenv("TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True  # Needed for voice channel events
intents.reactions = True  # Required for reaction events
bot = commands.Bot(command_prefix='!', intents=intents)

# Global dictionary to store active paginated messages for the !list command
active_list_messages = {}

@bot.event
async def on_ready():
  change_status.start()
  await g.setup(bot)  # Load the CountryGuesser cog
  print("Your bot is ready")


@tasks.loop(seconds=10)
async def change_status():
    # this is a joke status, don't take it seriously, can be changed to anything
    activity = discord.CustomActivity(name="Laf Sokarım Derinden Aklın Oynar Yerinden")
    await bot.change_presence(activity=activity)


@bot.event  
async def on_message(message):
    if message.author == bot.user:  # Prevent bot from responding to itself
        return

    content_lower = message.content.lower()
    
    if content_lower == '!list':
        try:
            with open('countries.txt', 'r') as f:
                lines = f.readlines()
            
            if not lines:
                await message.channel.send("The countries list is empty.")
                return

            lines_per_page = 15  # Number of lines per page
            pages = []
            current_page_content = ""
            current_line_count = 0

            for line in lines:
                if len(current_page_content) + len(line) + 100 > 1900 or current_line_count >= lines_per_page:
                    if current_page_content: # Ensure not adding empty initial page
                        pages.append(f"```\n{current_page_content.strip()}\n```")
                    current_page_content = ""
                    current_line_count = 0
                
                current_page_content += line
                current_line_count += 1
            
            if current_page_content.strip(): # Add the last page if it has content
                pages.append(f"```\n{current_page_content.strip()}\n```")

            if not pages:
                 await message.channel.send("The countries list is empty or could not be paginated.")
                 return

            current_page_index = 0
            page_content_to_send = f"**Page {current_page_index + 1}/{len(pages)}**\n{pages[current_page_index]}"
            sent_message = await message.channel.send(page_content_to_send)

            if len(pages) > 1:
                active_list_messages[sent_message.id] = {
                    'pages': pages,
                    'current_index': current_page_index,
                    'author_id': message.author.id # Store original author to restrict control if needed
                }
                await sent_message.add_reaction('⬅️')
                await sent_message.add_reaction('➡️')
            
        except FileNotFoundError:
            await message.channel.send("Error: `countries.txt` not found.")
        except Exception as e:
            await message.channel.send(f"An error occurred: {e}")
        return # Handled

    if message.content.startswith('!help'):  # Handle !help command
        commands = """**Available Commands:**
        
**Commands:**
`!help` - Show this help menu
`!list` - Show the list of all available countries
`!g` - Start a street view guessing game (shows North, East, South, West views)
`!hint` - Get an extra hint for the guessing game (shows a random view), hint
`!stop_g` - Stop the current game.
`!eu` - Displays a map of Europe and beyond showing incorrectly guessed countries
`!as` - Displays a map of Asia and beyond showing incorrectly guessed countries
`!af` - Displays a map of Africa and beyond showing incorrectly guessed countries
`!am` - Displays a map of America and beyond showing incorrectly guessed countries
        """
        await message.channel.send(commands)
        return  # Handled, no further processing needed

    await bot.process_commands(message)  # Add this line to process commands from cogs

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot: # Ignore reactions from the bot itself
        return

    if reaction.message.id in active_list_messages:
        message_data = active_list_messages[reaction.message.id]
        pages = message_data['pages']
        current_index = message_data['current_index']
        # Optional: Check if reaction.user.id == message_data['author_id'] to restrict to original user

        new_index = current_index
        if reaction.emoji == '⬅️':
            new_index = max(0, current_index - 1)
        elif reaction.emoji == '➡️':
            new_index = min(len(pages) - 1, current_index + 1)
        else: # Not a navigation emoji we care about for this message
            return

        if new_index != current_index:
            message_data['current_index'] = new_index
            page_content_to_send = f"**Page {new_index + 1}/{len(pages)}**\n{pages[new_index]}"
            await reaction.message.edit(content=page_content_to_send)
        
        # Remove the user's reaction to allow them to click again easily
        try:
            await reaction.remove(user)
        except discord.Forbidden:
            # Bot might not have permissions to remove reactions
            pass 
        except discord.NotFound: 
            # Reaction might have been removed by someone else already
            pass

if __name__ == '__main__':
  keep_alive()
  bot.run(TOKEN)