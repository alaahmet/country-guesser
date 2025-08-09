import discord
from discord.ext import commands
import requests
import random
import os
import asyncio
from dotenv import load_dotenv
import plotly.express as px
import plotly.io as pio
import geopandas as gpd
import io
import json


load_dotenv()

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

COUNTRY_NAME_TO_CODE = {}
COUNTRY_CODE_TO_NAME = {}


COUNTRY_BOUNDS = {}

WORLD_GEOJSON_PATH = "data/ne_admin_0_map_units_50m.geojson"

eu_countries = []
as_countries = []
af_countries = []
am_countries = []

def load_country_data():
    """Load country metadata and continent groupings from project files."""
    try:
        with open("countries.txt", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split('\t')
                if len(parts) == 2:
                    name, code = parts[0].strip(), parts[1].strip().lower()
                    COUNTRY_NAME_TO_CODE[name.lower()] = code
                    COUNTRY_CODE_TO_NAME[code] = name
                else:
                    print(f"Error in countries.txt: {line.strip()}")
        print(f"Loaded {len(COUNTRY_NAME_TO_CODE)} country name mappings.")
        print(f"Loaded {len(COUNTRY_CODE_TO_NAME)} country code mappings.")
        
    except FileNotFoundError:
        print("Error: countries.txt not found. Country name guessing will not work.")
    except Exception as e:
        print(f"Error loading countries.txt: {e}")
        
    
    # Load country bounds data
    try:
        with open("country_bounds.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if len(parts) >= 5:  # code, south, west, north, east
                    try:
                        code = parts[0].lower()
                        south = float(parts[1])
                        west = float(parts[2])
                        north = float(parts[3])
                        east = float(parts[4])
                        COUNTRY_BOUNDS[code] = [south, west, north, east]
                    except ValueError:
                        print(f"Error parsing coordinates in country_bounds.txt: {line}")
                else:
                    print(f"Invalid format in country_bounds.txt: {line}")
        print(f"Loaded {len(COUNTRY_BOUNDS)} country bounds.")
    except FileNotFoundError:
        print("Warning: country_bounds.txt not found. Using default world bounds for all countries.")
    except Exception as e:
        print(f"Error loading country_bounds.txt: {e}")

    global eu_countries, as_countries, af_countries, am_countries

    try:
        with open("continents.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        eu_countries = [code.lower() for code in data.get("Europe", [])]
        as_countries = [code.lower() for code in data.get("Asia", [])]
        af_countries = [code.lower() for code in data.get("Africa", [])]
        am_countries = [code.lower() for code in data.get("America", [])]
        print(f"Loaded Europe countries: {len(eu_countries)}")
        print(f"Loaded Asia countries: {len(as_countries)}")
        print(f"Loaded Africa countries: {len(af_countries)}")
        print(f"Loaded America countries: {len(am_countries)}")
    except FileNotFoundError:
        print("CRITICAL ERROR: continents.json not found. Continent-specific data (e.g., for /eu map) will not be available.")
    except json.JSONDecodeError as e:
        print(f"CRITICAL ERROR: Failed to decode continents.json: {e}. Continent-specific data will not be available.")
    except Exception as e:
        print(f"CRITICAL ERROR: An unexpected error occurred while loading continents.json: {e}. Continent-specific data will not be available.")


class CountryGuesser(commands.Cog, name="CountryGuesser"):
    def __init__(self, bot):
        self.bot = bot
        self.current_game = None
        self.max_retries_location = 30
        
        self.incorrect_guesses = set()
        self._hint_cooldowns = {}
        self.hint_cooldown_seconds = 4
        
        self.view_directions = [
            {"heading": 0, "name": "North"},
            {"heading": 90, "name": "East"},
            {"heading": 180, "name": "South"},
            {"heading": 270, "name": "West"}
        ]
        
        self.world_gdf = None
        try:
            self.world_gdf = gpd.read_file(WORLD_GEOJSON_PATH)
            print(f"Loaded world GeoJSON data with {len(self.world_gdf)} countries")
            # Fix a few known ISO_A2 mismatches in the ne_admin_0_map_units_50m.geojson file
            corrections = {"Norway": "NO", "Réunion": "RE"}
            for name, correct_iso_a2 in corrections.items():
                mask = self.world_gdf['NAME'] == name
                if mask.any():
                    current_iso_a2 = self.world_gdf.loc[mask, 'ISO_A2'].iloc[0]
                    if current_iso_a2 != correct_iso_a2:
                        self.world_gdf.loc[mask, 'ISO_A2'] = correct_iso_a2
                        print(f"Corrected ISO_A2 for {name} from '{current_iso_a2}' to '{correct_iso_a2}'.")
                else:
                    print(f"Warning: '{name}' not found in GeoJSON for ISO_A2 correction.")


        except Exception as e:
            print(f"Error loading world GeoJSON: {e}")
            print("Map plotting functionality will be unavailable")

    async def _fetch_url_json(self, url):
        """Fetch JSON from a URL using a thread executor to avoid blocking."""
        try:
            # Ensure timeout is passed as a keyword argument
            response = await self.bot.loop.run_in_executor(
                None, lambda: requests.get(url, timeout=10)
            )
            response.raise_for_status()  # Raise an exception for HTTP errors
            return response.json()
        except requests.exceptions.Timeout:
            print(f"Request timed out: {url}")
            return None
        except requests.exceptions.RequestException as e:
            print(f"API request failed: {e}")
            return None
        except ValueError as e: # Handles JSON decoding errors
            print(f"JSON decoding failed: {e} for URL: {url}")
            return None

    def _get_street_view_image_urls(self, pano_id):
        """Generate URLs for the 4 cardinal directions of a Street View panorama."""
        urls = []
        for direction in self.view_directions:
            url = (
                f"https://maps.googleapis.com/maps/api/streetview?"
                f"size=1200x675&pano={pano_id}&heading={direction['heading']}&key={GOOGLE_MAPS_API_KEY}"
            )
            urls.append({"url": url, "name": direction["name"]})
        return urls
        
    async def _get_street_view_in_country(self, country_code):
        """Find a random Street View location within the given country."""
        if not GOOGLE_MAPS_API_KEY:
            print("Error: GOOGLE_MAPS_API_KEY is not set.")
            return None
            
        country_name = COUNTRY_CODE_TO_NAME.get(country_code.lower(), country_code)
        
        default_bounds = [-90, -180, 90, 180]
        
        bounds = COUNTRY_BOUNDS.get(country_code.lower(), default_bounds)
        south, west, north, east = bounds
        
        print(f"Searching for Street View in {country_name} within bounds: {bounds}")
        
        radii = [1000, 10000, 100000, 1000000]

        for attempt in range(self.max_retries_location):
            lat = random.uniform(south, north)
            lng = random.uniform(west, east)
            
            # Cycle through radii based on attempt number
            radius = radii[attempt % len(radii)]
            metadata_url = (
                f"https://maps.googleapis.com/maps/api/streetview/metadata?"
                f"location={lat},{lng}&radius={radius}&source=outdoor&key={GOOGLE_MAPS_API_KEY}"
            )
            
            metadata = await self._fetch_url_json(metadata_url)
            
            if metadata and metadata.get("status") == "OK" and metadata.get("pano_id"):
                pano_id = metadata["pano_id"]
                actual_lat = metadata["location"]["lat"]
                actual_lng = metadata["location"]["lng"]
                
                geocode_url = (
                    f"https://maps.googleapis.com/maps/api/geocode/json?"
                    f"latlng={actual_lat},{actual_lng}&key={GOOGLE_MAPS_API_KEY}"
                )
                geocode_data = await self._fetch_url_json(geocode_url)

                print(f"Attempt {attempt + 1}: Found Street View at {actual_lat}, {actual_lng} with radius {radius}.")
                if geocode_data and geocode_data.get("status") == "OK" and geocode_data.get("results"):
                    for component in geocode_data["results"][0].get("address_components", []):
                        if "country" in component.get("types", []):
                            found_country_code = component.get("short_name", "").lower()
                            if found_country_code == country_code.lower():
                                print(f"Found valid Street View in {country_name} at {actual_lat}, {actual_lng} with radius {radius}.")
                                return {
                                    "pano_id": pano_id,
                                    "country_code": country_code.lower(),
                                    "country_name": country_name,
                                    "lat": actual_lat,
                                    "lng": actual_lng,
                                }
                            else:
                                print(
                                    f"Found Street View at {actual_lat}, {actual_lng} (radius {radius}) "
                                    f"but country code {found_country_code} != {country_code}."
                                )
            await asyncio.sleep(0.1)  # Small delay between attempts
        
        print(f"Failed to find a suitable Street View location in {country_name} after {self.max_retries_location} attempts.")
        return None

    async def _process_guess(self, channel: discord.TextChannel, author: discord.User, original_message: discord.Message, guess_input: str):
        """Process a guess, react, and end the game if correct."""
        normalized_guess = guess_input.strip().lower()
        if not self.current_game:
            await channel.send("Error: No game active to process guess for.", delete_after=10)
            return

        correct_code = self.current_game["country_code"]
        correct_name = self.current_game["country_name"]
        
        guessed_code = None

        if len(normalized_guess) == 2 and normalized_guess.isalpha():
            guessed_code = normalized_guess
        elif normalized_guess in COUNTRY_NAME_TO_CODE:
            guessed_code = COUNTRY_NAME_TO_CODE[normalized_guess]
        else:
            return

        # Add country flag reaction
        if guessed_code and len(guessed_code) == 2 and guessed_code.isalpha():
            try:
                flag_emoji = "".join(chr(ord(char.upper()) - ord('A') + 0x1F1E6) for char in guessed_code)
                await original_message.add_reaction(flag_emoji)
            except discord.Forbidden:
                print("Bot does not have permission to add reactions.")
            except discord.HTTPException as e:
                print(f"Failed to add flag reaction: {e}")

        if guessed_code == correct_code:
            try:
                await original_message.add_reaction('✅') # Add tick for correct guess
            except discord.Forbidden:
                print("Bot does not have permission to add reactions.")
            except discord.HTTPException as e:
                print(f"Failed to add reaction: {e}")

            winner = author
            street_view_image_url = (
                f"https://maps.googleapis.com/maps/api/streetview?"
                f"size=600x400&pano={self.current_game['pano_id']}&heading=0&key={GOOGLE_MAPS_API_KEY}"
            )
            
            map_link = f"https://www.google.com/maps/@?api=1&map_action=pano&pano={self.current_game['pano_id']}"
            
            embed = discord.Embed(
                title="🎉 Correct Guess! 🎉",
                description=f"{winner.mention} guessed it right! The country was **{correct_name} ({correct_code.upper()})**.",
                color=discord.Color.green()
            )
            embed.add_field(name="Location", value=f"[View on Google Maps]({map_link})")
            embed.set_image(url=street_view_image_url)
            embed.set_footer(text="Game Over!")
            await channel.send(embed=embed)
            
            self.incorrect_guesses = set()
            self.current_game = None  # End the game
        else:
            try:
                await original_message.add_reaction('❌') # Add cross for incorrect guess
            except discord.Forbidden:
                print("Bot does not have permission to add reactions.")
            except discord.HTTPException as e:
                print(f"Failed to add reaction: {e}")
            
            # Add to incorrect guesses set
            if guessed_code:
                self.incorrect_guesses.add(guessed_code)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if not self.current_game or self.current_game['channel_id'] != message.channel.id:
            return

        # Plain 'hint' (no prefix) during an active game
        if message.content.strip().lower() == "hint":
            await self._send_hint_impl(message.channel)
            return

        if message.content.startswith('!'):
            return

        await self._process_guess(message.channel, message.author, message, message.content)

    @commands.command(name="stop_g", help="Stops the current guessing game in this channel (requires manage_messages).")
    @commands.has_permissions(manage_messages=True)
    async def stop_guessing_game(self, ctx):
        if self.current_game and self.current_game['channel_id'] == ctx.channel.id:
            game_start_time = self.current_game.get('start_time')
            if not game_start_time or (discord.utils.utcnow() - game_start_time).total_seconds() < 60:
                await ctx.send("The game cannot be stopped until at least 1 minute has passed since it started.", delete_after=10)
                return

            pano_id = self.current_game['pano_id']
            country_code = self.current_game['country_code'].upper()
            country_name = self.current_game['country_name']
            
            street_view_image_url = (
                f"https://maps.googleapis.com/maps/api/streetview?"
                f"size=600x400&pano={pano_id}&heading=0&key={GOOGLE_MAPS_API_KEY}"
            )
            
            map_link = f"https://www.google.com/maps/@?api=1&map_action=pano&pano={pano_id}"
            
            embed = discord.Embed(
                title="Game Stopped",
                description=f"The game has been stopped by {ctx.author.mention}.",
                color=discord.Color.red()
            )
            
            embed.add_field(
                name="Correct Answer", 
                value=f"The correct country was **{country_name} ({country_code})**"
            )
            
            embed.add_field(
                name="Location", 
                value=f"[View on Google Maps]({map_link})"
            )
            
            embed.set_image(url=street_view_image_url)
            
            await ctx.send(embed=embed)
                
            self.current_game = None
        else:
            await ctx.send("No game is currently active in this channel to stop.")
            
    @stop_guessing_game.error
    async def stop_g_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You don't have permission to stop the game.", delete_after=10)
        else:
            print(f"Error in stop_g command: {error}")
            await ctx.send("An error occurred while trying to stop the game.", delete_after=10)

    @commands.command(name="g", help="Starts a new Street View guessing game with a 360° view.")
    @commands.cooldown(1, 5, commands.BucketType.channel) # 5 seconds cooldown
    async def start_guessing_game(self, ctx):
        if not GOOGLE_MAPS_API_KEY:
            await ctx.send("The Google Maps API key is not configured for the bot. Cannot start the game.")
            return
            

        if self.current_game:
            if self.current_game['channel_id'] == ctx.channel.id:
                await ctx.send("A game is already in progress in this channel! Use `<country_code>` or the country name to guess.")
            else:
                other_channel = self.bot.get_channel(self.current_game['channel_id'])
                await ctx.send(f"A game is already in progress in another channel ({other_channel.mention if other_channel else 'unknown channel'}). Please wait.")
            return

        msg = await ctx.send("🌍 Starting a new game... Choosing a country and finding a location, this might take a moment...")
        
        self.incorrect_guesses = set()
        
        if not COUNTRY_CODE_TO_NAME:
            await msg.edit(content="Error: Country data is not loaded. Cannot start the game.")
            return
            
        chosen_country_code = random.choice(list(COUNTRY_CODE_TO_NAME.keys()))
        chosen_country_name = COUNTRY_CODE_TO_NAME[chosen_country_code]
        
        location_data = await self._get_street_view_in_country(chosen_country_code)

        if not location_data:
            await msg.edit(content=f"Could not find a suitable Street View location in {chosen_country_name} after several attempts. Please try again later.")
            return

        self.current_game = {
            "channel_id": ctx.channel.id,
            "country_code": location_data["country_code"],
            "country_name": location_data["country_name"],
            "pano_id": location_data["pano_id"],
            "lat": location_data["lat"],
            "lng": location_data["lng"],
            "message": msg,
            "start_time": discord.utils.utcnow()
        }

        view_urls = self._get_street_view_image_urls(location_data['pano_id'])
        
        embed = discord.Embed(
            title="🌍 Guess the Location! 🌍",
            description="I've picked a location from one of the chosen countries. Can you guess which **country**?",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="How to Play",
            value="To make a guess either type `<2-letter_country_code>`  (e.g., `us`, `jp`) or the country name (e.g., `united states`, `japan`).",
            inline=False
        )
        
        embed.set_image(url=view_urls[0]["url"])
        embed.set_footer(text="360° view mode - Look at all 4 directions to help identify the location")
        
        await msg.edit(content=None, embed=embed)
        
        for i in range(1, 4):  # Skip the first one since it's in the main embed
            direction_embed = discord.Embed(
                title=f"View facing {view_urls[i]['name']}",
                color=discord.Color.blue()
            )
            direction_embed.set_image(url=view_urls[i]["url"])
            await ctx.send(embed=direction_embed)


    async def _show_continent_map(self, ctx, continent_name_display: str, specific_country_codes: list, plotly_map_scope: str, auto_fit_bounds: bool = False):
        """Render a choropleth map for a continent, highlighting incorrect guesses."""
        if self.world_gdf is None:
            await ctx.send("Sorry, the world map data couldn't be loaded. Map visualization is unavailable.")
            return

        if not specific_country_codes:
            await ctx.send(f"No country data loaded for {continent_name_display}. Cannot generate map.")
            return

        continent_gdf = self.world_gdf.copy()
        
        continent_gdf = continent_gdf[continent_gdf['ISO_A2'].str.lower().isin(specific_country_codes)]

        if continent_gdf.empty:
            await ctx.send(f"No map data found for countries listed under {continent_name_display}. "
                           f"Ensure `continents.json` and GeoJSON data are correct and include these countries.")
            return
        
        def get_country_status(country_code_iso_a2):
            code_lower = country_code_iso_a2.lower()
            if code_lower in self.incorrect_guesses:
                return "Incorrect Guess"
            else:
                return "Not Guessed"
                
        continent_gdf['status'] = continent_gdf['ISO_A2'].apply(get_country_status)
        
        map_title = f"{continent_name_display} Map"
        
        fig = px.choropleth(
            continent_gdf,
            geojson=continent_gdf.geometry,
            locations=continent_gdf.index, # Use GeoDataFrame index
            color='status',
            color_discrete_map={
                'Incorrect Guess': '#FF4757', # Modern red
                'Not Guessed': '#57606F'       # Sophisticated dark gray
            },
            scope=plotly_map_scope,
            labels={'status': 'Country Status'},
            title=map_title
        )
        
        fig.update_geos(
            showcoastlines=True,
            coastlinecolor="#2C3E50",
            coastlinewidth=1.5,
            showland=True,
            landcolor="#ECEFF1",
            showocean=True,
            oceancolor="#E3F2FD",
            showlakes=True,
            lakecolor="#E1F5FE",
            showrivers=True,
            rivercolor="#81D4FA",
            riverwidth=0.5,
            showcountries=True,
            countrycolor="#BDBDBD",
            countrywidth=0.8,
            projection_type="natural earth"
        )

        if auto_fit_bounds and plotly_map_scope == "world":
            fig.update_geos(fitbounds="locations", visible=True)
        
        fig.update_layout(
            height=900,
            width=1200,
            margin={"r":20,"t":80,"l":20,"b":20},
            title={
                'text': map_title,
                'x': 0.5,
                'xanchor': 'center',
                'font': {
                    'size': 24,
                    'family': 'Arial, sans-serif',
                    'color': '#2C3E50'
                }
            },
            font=dict(
                family="Arial, sans-serif",
                size=14,
                color="#2C3E50"
            ),
            paper_bgcolor="#FAFAFA",
            plot_bgcolor="#FAFAFA",
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=-0.1,
                xanchor="center",
                x=0.5,
                bgcolor="rgba(255,255,255,0.8)",
                bordercolor="#BDBDBD",
                borderwidth=1,
                font=dict(size=12)
            )
        )
        
        game_status_text = "No active game"
        if self.current_game:
            game_status_text = "Game in progress"
        
        total_continent_countries = len(specific_country_codes)
        continent_incorrect_guesses = {code for code in self.incorrect_guesses if code in specific_country_codes}
        not_guessed_count = total_continent_countries - len(continent_incorrect_guesses)
        
        footer_text_status = f"Status: {game_status_text} - {not_guessed_count}/{total_continent_countries} countries in {continent_name_display} not guessed"
        
        fig.update_layout(
            annotations=[
                dict(
                    x=0.02,
                    y=0.02,
                    xref="paper",
                    yref="paper",
                    text=f"<b style='color:#2C3E50;'>{footer_text_status}</b>",
                    showarrow=False,
                    font=dict(
                        size=16,
                        family="Arial, sans-serif",
                        color="#2C3E50"
                    ),
                    bgcolor="rgba(255,255,255,0.9)",
                    bordercolor="#BDBDBD",
                    borderwidth=1,
                    borderpad=8
                )
            ]
        )
        
        img_bytes = io.BytesIO()
        pio.write_image(fig, img_bytes, format="png", width=1200, height=900, scale=2)
        img_bytes.seek(0)
        
        discord_file = discord.File(img_bytes, filename=f"{continent_name_display.lower().replace(' ', '_')}_map.png")
        
        embed = discord.Embed(
            title=f"🗺️ {map_title}",
            description="🔴 Incorrect guesses • ⚫ Not yet guessed\n*Map of your guessing progress*",
            color=0x3498DB  # Modern blue color
        )
        embed.set_image(url=f"attachment://{discord_file.filename}")
        embed.set_footer(text=f"🎯 {footer_text_status}")
        
        await ctx.send(file=discord_file, embed=embed)


    async def _send_hint_impl(self, channel: discord.TextChannel):
        # Cooldown per channel for plain 'hint' and command reuse
        now = discord.utils.utcnow()
        last_used = self._hint_cooldowns.get(channel.id)
        if last_used is not None:
            elapsed = (now - last_used).total_seconds()
            remaining = self.hint_cooldown_seconds - elapsed
            if remaining > 0:
                await channel.send(f"Hint is on cooldown. Try again in {int(remaining)}s.", delete_after=5)
                return

        if not self.current_game or self.current_game['channel_id'] != channel.id:
            await channel.send("No active game. Start one with `!g`.", delete_after=10)
            return

        # Record cooldown time
        self._hint_cooldowns[channel.id] = now

        new_location_data = await self._get_street_view_in_country(self.current_game["country_code"]) 
        if new_location_data:
            new_view_urls = self._get_street_view_image_urls(new_location_data['pano_id'])
            for i in range(4):
                direction_embed = discord.Embed(
                    title=f"Extra View facing {new_view_urls[i]['name']}",
                    color=discord.Color.purple()
                )
                direction_embed.set_image(url=new_view_urls[i]["url"])
                await channel.send(embed=direction_embed)
        else:
            await channel.send("Couldn't find an extra location right now. Try `!hint` again.")


    @commands.command(name="hint", help="Shows another 360° location from the current country.")
    @commands.cooldown(1, 4, commands.BucketType.channel)
    async def send_hint(self, ctx):
        await self._send_hint_impl(ctx.channel)



    
    @commands.command(name="eu", help="Displays a Europe map highlighting incorrect guesses.")
    async def show_europe_map(self, ctx):
        """Map of Europe with incorrect guesses highlighted."""
        await self._show_continent_map(ctx, "Europe", eu_countries, "europe")

    @commands.command(name="as", help="Displays an Asia map highlighting incorrect guesses.")
    async def show_asia_map(self, ctx):
        """Map of Asia with incorrect guesses highlighted."""
        await self._show_continent_map(ctx, "Asia", as_countries, "asia")

    @commands.command(name="af", help="Displays an Africa map highlighting incorrect guesses.")
    async def show_africa_map(self, ctx):
        """Map of Africa with incorrect guesses highlighted."""
        await self._show_continent_map(ctx, "Africa", af_countries, "africa")

    @commands.command(name="am", help="Displays an Americas map highlighting incorrect guesses.")
    async def show_americas_map(self, ctx):
        """Map of the Americas with incorrect guesses highlighted."""
        await self._show_continent_map(ctx, "The Americas", am_countries, "world", auto_fit_bounds=True)


async def setup(bot):
    if not GOOGLE_MAPS_API_KEY:
        print("CRITICAL: GOOGLE_MAPS_API_KEY environment variable is not set. CountryGuesser cog will not be loaded.")
        print("Please set the GOOGLE_MAPS_API_KEY environment variable with your Google Maps API key.")
        return  # Exit early if the API key is not set

    load_country_data()
    await bot.add_cog(CountryGuesser(bot))
    print("CountryGuesser cog loaded.")
