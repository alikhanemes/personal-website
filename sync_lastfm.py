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
        return response.json().get("access_token")
    except Exception as e:
        print(f"Spotify Auth Error: {e}")
        return None

def clean_name(text):
    text = re.sub(r'\s*[\(\[].*?[\)\]]', '', text)
    return text.split(' - ')[0].strip()

def get_spotify_data(artist, track, token):
    try:
        q = f"track:{clean_name(track)} artist:{artist}"
        url = f"https://api.spotify.com/v1/search?q={q}&type=track&limit=1"
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(url, headers=headers).json()
        if 'tracks' in resp and resp['tracks']['items']:
            item = resp['tracks']['items'][0]
            return item['album']['images'][0]['url'], item['external_urls']['spotify']
    except:
        pass
    return None, None

def fetch_from_lastfm(period, token):
    print(f"--- Fetching {period} from Last.fm ---")
    url = f"http://ws.audioscrobbler.com/2.0/?method=user.gettoptracks&user={LASTFM_USERNAME}&api_key={LASTFM_API_KEY}&format=json&period={period}&limit=50"
    
    try:
        data = requests.get(url).json()
        raw_tracks = data['toptracks']['track']
        print(f"Found {len(raw_tracks)} tracks.")
    except Exception as e:
        print(f"Error fetching from Last.fm: {e}")
        return []

    processed = []
    for t in raw_tracks:
        artist = t['artist']['name']
        title = t['name']
        print(f"Processing: {title}")
        
        img, link = get_spotify_data(artist, title, token)
        
        # Fallbacks
        if not img:
            try: img = t['image'][-1]['#text']
            except: img = ""
        if not link:
            link = t['url']

        processed.append({
            "rank": t['@attr']['rank'],
            "title": title,
            "artist": artist,
            "playcount": int(t.get('playcount', 0)),
            "image": img,
            "link": link
        })
        time.sleep(0.05) # Tiny sleep to avoid rate limits
    return processed

def sync_lastfm():
    # 1. Get Token
    token = get_spotify_token()
    if not token:
        print("FAIL: No Spotify Token. Check Client ID/Secret.")
        return

    # 2. Fetch Data
    recent = fetch_from_lastfm('7day', token)
    alltime = fetch_from_lastfm('overall', token)

    # 3. THE SAVE (Force Save)
    final_data = {
        "recent": recent,
        "alltime": alltime
    }

    print(f"DEBUG: Attempting to write to {os.path.abspath('songs.json')}")
    
    try:
        with open('songs.json', 'w', encoding='utf-8') as f:
            json.dump(final_data, f, indent=4, ensure_ascii=False)
        print("✅ SUCCESS: songs.json has been written!")
    except Exception as e:
        print(f"❌ ERROR WRITING FILE: {e}")

if __name__ == "__main__":
    sync_lastfm()