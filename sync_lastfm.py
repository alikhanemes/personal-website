import os
import json
import requests
import time
import re
from urllib.parse import quote
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
    """Removes common suffixes that break Spotify search queries."""
    # Remove text in brackets/parentheses: (Remastered), [Deluxe Edition], etc.
    text = re.sub(r'\s*[\(\[].*?[\)\]]', '', text)
    # Remove common tags after a dash
    text = re.split(r' - | – ', text)[0]
    # Specific common terms
    text = re.sub(r'(?i)\b(remastered|remaster|deluxe|version|edit|radio edit|live)\b', '', text)
    return text.strip()

def get_spotify_data(artist, track, token):
    """Attempts a strict search first, then a fuzzy search to get the Spotify Image."""
    headers = {"Authorization": f"Bearer {token}"}
    cleaned_track = clean_name(track)
    
    queries = [
        f"track:\"{cleaned_track}\" artist:\"{artist}\"", # Strict
        f"{cleaned_track} {artist}"                      # Fuzzy/General
    ]

    for q in queries:
        try:
            url = f"https://api.spotify.com/v1/search?q={quote(q)}&type=track&limit=1"
            resp = requests.get(url, headers=headers).json()
            if 'tracks' in resp and resp['tracks']['items']:
                item = resp['tracks']['items'][0]
                # Try to get the largest image (index 0)
                img_url = item['album']['images'][0]['url'] if item['album']['images'] else None
                spotify_url = item['external_urls']['spotify']
                return img_url, spotify_url
        except Exception:
            continue
            
    return None, None

def fetch_from_lastfm(period, token):
    print(f"--- Fetching {period} from Last.fm ---")
    url = f"http://ws.audioscrobbler.com/2.0/?method=user.gettoptracks&user={LASTFM_USERNAME}&api_key={LASTFM_API_KEY}&format=json&period={period}&limit=50"
    
    try:
        data = requests.get(url).json()
        raw_tracks = data['toptracks']['track']
    except Exception as e:
        print(f"Error fetching from Last.fm: {e}")
        return []

    processed = []
    for t in raw_tracks:
        artist = t['artist']['name']
        title = t['name']
        
        # Priority: Fetch from Spotify
        img, link = get_spotify_data(artist, title, token)
        
        if img:
            print(f"✅ Found Spotify Image for: {title}")
        else:
            print(f"⚠️  No Spotify match, falling back for: {title}")
            # Fallback to Last.fm metadata if Spotify fails
            try:
                img = t['image'][-1]['#text'] # Last.fm's largest size
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
        time.sleep(0.1) # Moderate sleep to respect rate limits
    return processed

def sync_lastfm():
    token = get_spotify_token()
    if not token:
        print("FAIL: No Spotify Token. Check Client ID/Secret.")
        return

    recent = fetch_from_lastfm('7day', token)
    alltime = fetch_from_lastfm('overall', token)

    final_data = {
        "recent": recent,
        "alltime": alltime,
        "updated_at": int(time.time())
    }

    try:
        with open('songs.json', 'w', encoding='utf-8') as f:
            json.dump(final_data, f, indent=4, ensure_ascii=False)
        print(f"SUCCESS: {len(recent) + len(alltime)} tracks synced to songs.json!")
    except Exception as e:
        print(f"ERROR WRITING FILE: {e}")

if __name__ == "__main__":
    sync_lastfm()
