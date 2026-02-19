import json
import os
import time
from flask import Flask, render_template
import sync_lastfm  # Import your sync script so we can use it here

app = Flask(__name__)

# CONFIGURATION:
# How many seconds to wait before fetching new data?
# Set to 0 if you want it to fetch literally every time (warning: will be slow).
CACHE_TIMEOUT = 600

def get_fresh_data():
    """Checks if data is old and updates it if necessary."""
    should_update = False
    
    # 1. Check if file exists
    if not os.path.exists('songs.json'):
        should_update = True
    else:
        # 2. Check if file is too old
        last_modified = os.path.getmtime('songs.json')
        current_time = time.time()
        
        if (current_time - last_modified) > CACHE_TIMEOUT:
            should_update = True
            
    # 3. Run the update if needed
    if should_update:
        try:
            print("Data is stale (or missing). Running auto-sync...")
            sync_lastfm.sync_lastfm() # This calls the function from your other file
        except Exception as e:
            print(f"Error auto-fetching data: {e}")
            # If fetch fails, we just continue and try to load whatever old data we have

def load_songs():
    # First, ensure we have fresh data
    get_fresh_data()

    # Then load the file as usual
    if os.path.exists('songs.json'):
        try:
            with open('songs.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Handle case where file might be in old format (list vs dict)
                if isinstance(data, list):
                    return {"recent": data, "alltime": []}
                return data
        except:
            return {"recent": [], "alltime": []}
    return {"recent": [], "alltime": []}

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/music')
def music():
    song_data = load_songs()
    return render_template('music.html', songs=song_data)

if __name__ == '__main__':
    app.run(debug=True)