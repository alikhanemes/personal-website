import os
import json
import requests
import time
import re
from dotenv import load_dotenv

load_dotenv()

LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
LASTFM_USERNAME = os.getenv("LASTFM_USERNAME")
SPOTIFY_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

def get_spotify_token():
    url = "https://accounts.spotify.com/api/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": SPOTIFY_ID,
        "client_secret": SPOTIFY_SECRET
    }
    try:
        response = requests.post(url, data=data)
        token_data = response.json()
        return token_data.get("access_token")
    except Exception as e:
        print(f"Spotify Auth Exception: {e}")
        return None

def clean_name(text):
    text = re.sub(r'\s*[\(\[].*?[\)\]]', '', text)
    return text.strip()

def get_spotify_data(artist, track, token):
    headers = {"Authorization": f"Bearer {token}"}
    search_query = f"{artist} {clean_name(track)}"
    url = "https://api.spotify.com/v1/search"
    params = {"q": search_query, "type": "track", "limit": 1}

    try:
        resp = requests.get(url, headers=headers, params=params)
        
        # Handle Rate Limiting (429)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 5))
            print(f"⚠️ Rate limited! Sleeping for {retry_after}s...")
            time.sleep(retry_after)
            return get_spotify_data(artist, track, token) # Retry
            
        if resp.status_code != 200:
            return None, None

        data = resp.json()
        if 'tracks' in data and data['tracks']['items']:
            item = data['tracks']['items'][0]
            img_url = item['album']['images'][0]['url'] if item['album']['images'] else None
            spotify_url = item['external_urls']['spotify']
            return img_url, spotify_url
            
    except Exception as e:
        print(f"Request Error: {e}")
        
    return None, None

def fetch_raw_lastfm_tracks(period):
    url = f"http://ws.audioscrobbler.com/2.0/?method=user.gettoptracks&user={LASTFM_USERNAME}&api_key={LASTFM_API_KEY}&format=json&period={period}&limit=50"
    try:
        resp = requests.get(url)
        data = resp.json()
        return data.get('toptracks', {}).get('track', [])
    except:
        return []

def load_existing_cache():
    """Load existing songs.json to avoid re-searching Spotify for known songs."""
    cache = {}
    if os.path.exists('songs.json'):
        try:
            with open('songs.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Combine both lists into a dictionary for easy lookup
                for category in ['recent', 'alltime']:
                    for track in data.get(category, []):
                        key = f"{track['artist']}-{track['title']}".lower()
                        cache[key] = {"image": track['image'], "link": track['link']}
        except Exception as e:
            print(f"Cache load error: {e}")
    return cache

def sync_lastfm():
    token = get_spotify_token()
    if not token:
        print("FAIL: No Spotify Token.")
        return

    # 1. Load existing data to save API calls
    cache = load_existing_cache()
    print(f"Loaded {len(cache)} tracks from cache.")

    # 2. Fetch raw data from Last.fm
    raw_recent = fetch_raw_lastfm_tracks('7day')
    raw_alltime = fetch_raw_lastfm_tracks('overall')

    # 3. Process data
    def process_list(raw_list):
        processed = []
        for t in raw_list:
            artist = t['artist']['name']
            title = t['name']
            cache_key = f"{artist}-{title}".lower()

            # Check cache first
            if cache_key in cache:
                img = cache[cache_key]['image']
                link = cache[cache_key]['link']
            else:
                # Only call Spotify if not in cache
                img, link = get_spotify_data(artist, title, token)
                if not img:
                    try: img = t['image'][-1]['#text']
                    except: img = ""
                    link = t['url']
                # Add to cache for this run (in case song is in both lists)
                cache[cache_key] = {"image": img, "link": link}
                print(f"📡 Spotify Search: {title}")
                time.sleep(0.2) # Small delay to be safe

            processed.append({
                "rank": t['@attr']['rank'],
                "title": title,
                "artist": artist,
                "playcount": int(t.get('playcount', 0)),
                "image": img,
                "link": link
            })
        return processed

    print("Processing Recent...")
    recent_processed = process_list(raw_recent)
    print("Processing All-Time...")
    alltime_processed = process_list(raw_alltime)

    # 4. Save
    final_data = {
        "recent": recent_processed,
        "alltime": alltime_processed,
        "last_updated": int(time.time())
    }

    with open('songs.json', 'w', encoding='utf-8') as f:
        json.dump(final_data, f, indent=4, ensure_ascii=False)
    print("\n🎉 SUCCESS: songs.json updated!")

if __name__ == "__main__":
    sync_lastfm()
