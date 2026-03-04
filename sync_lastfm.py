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
        if "access_token" not in token_data:
            print(f"Auth Error Details: {token_data}")
            return None
        return token_data.get("access_token")
    except Exception as e:
        print(f"Spotify Auth Exception: {e}")
        return None

def clean_name(text):
    # Remove (Remastered), [Official Video], etc.
    text = re.sub(r'\s*[\(\[].*?[\)\]]', '', text)
    # Remove extra whitespace
    return text.strip()

def get_spotify_data(artist, track, token):
    headers = {"Authorization": f"Bearer {token}"}
    search_query = f"{artist} {clean_name(track)}"
    url = "https://api.spotify.com/v1/search"
    params = {"q": search_query, "type": "track", "limit": 1}

    try:
        resp = requests.get(url, headers=headers, params=params)
        
        # If we hit a rate limit, read the 'Retry-After' header
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 3)) # Default to 3 seconds
            print(f"⚠️ Rate limited! Sleeping for {retry_after}s...")
            time.sleep(retry_after)
            return get_spotify_data(artist, track, token) # Try again after sleeping

        if resp.status_code == 401:
            print("Token expired or invalid.")
            return None, None
        
        if resp.status_code != 200:
            print(f"Spotify API Error {resp.status_code}: {resp.text}")
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

def fetch_from_lastfm(period, token):
    print(f"\n--- Fetching {period} from Last.fm ---")
    url = f"http://ws.audioscrobbler.com/2.0/?method=user.gettoptracks&user={LASTFM_USERNAME}&api_key={LASTFM_API_KEY}&format=json&period={period}&limit=50"
    
    try:
        resp = requests.get(url)
        data = resp.json()
        if 'toptracks' not in data:
            print(f"Last.fm Error: {data}")
            return []
        raw_tracks = data['toptracks']['track']
    except Exception as e:
        print(f"Error connecting to Last.fm: {e}")
        return []

    processed = []
    for t in raw_tracks:
        artist = t['artist']['name']
        title = t['name']
        
        # Try to get Spotify Data
        img, link = get_spotify_data(artist, title, token)
        
        if img:
            print(f"✅ Found: {title} by {artist}")
        else:
            print(f"❌ Failed: {title} (Using Last.fm fallback)")
            # Fallback to Last.fm Image if Spotify fails
            try:
                img = t['image'][-1]['#text']
            except:
                img = ""
            link = t['url']

        processed.append({
            "rank": t['@attr']['rank'],
            "title": title,
            "artist": artist,
            "playcount": int(t.get('playcount', 0)),
            "image": img,
            "link": link
        })
        time.sleep(0.5) # Avoid hitting Spotify rate limits
    return processed

def sync_lastfm():
    # 1. Get Token
    token = get_spotify_token()
    if not token:
        print("FAIL: Could not get Spotify Token. Check your .env file.")
        return

    # 2. Fetch Data
    recent = fetch_from_lastfm('7day', token)
    alltime = fetch_from_lastfm('overall', token)

    # 3. Save
    final_data = {
        "recent": recent,
        "alltime": alltime,
        "last_updated": int(time.time())
    }

    try:
        with open('songs.json', 'w', encoding='utf-8') as f:
            json.dump(final_data, f, indent=4, ensure_ascii=False)
        print("\n🎉 SUCCESS: songs.json is updated!")
    except Exception as e:
        print(f"❌ WRITE ERROR: {e}")

if __name__ == "__main__":
    sync_lastfm()
