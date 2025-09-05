import os
import json
import threading
import time
from datetime import datetime, timedelta
import logging
from flask import Flask, request, jsonify, Response, render_template_string, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from ytmusicapi import YTMusic
import yt_dlp
from functools import wraps

# --- Genel Ayarlar ve Başlatma ---
app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
logging.basicConfig(level=logging.INFO)

# Gerekli dosyaların ve klasörlerin yolları
DATA_DIR = 'data'
USERS_DIR = os.path.join(DATA_DIR, 'Users')
PASSWORDS_FILE = os.path.join(DATA_DIR, 'passwords.json')
LINKS_FILE = os.path.join(DATA_DIR, 'links.json')
POPULAR_FILE = os.path.join(DATA_DIR, 'popular.json')
ADMIN_CONFIG_FILE = os.path.join(DATA_DIR, 'admin_config.json')

# Gerekli klasörleri ve dosyaları oluştur
os.makedirs(USERS_DIR, exist_ok=True)
for f in [PASSWORDS_FILE, LINKS_FILE, POPULAR_FILE, ADMIN_CONFIG_FILE]:
    if not os.path.exists(f):
        with open(f, 'w') as file:
            json.dump({} if 'passwords' in f or 'config' in f else [], file)


# YTMusic API istemcisi
try:
    ytmusic = YTMusic()
except Exception as e:
    logging.error(f"YTMusic API başlatılamadı: {e}")
    ytmusic = None

# --- Yardımcı Fonksiyonlar ve Veri Yönetimi ---

def read_json(file_path, default_data=None):
    """JSON dosyasını güvenli bir şekilde okur."""
    try:
        if not os.path.exists(file_path):
            return default_data if default_data is not None else {}
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return default_data if default_data is not None else {}

def write_json(file_path, data):
    """JSON dosyasına güvenli bir şekilde yazar."""
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logging.error(f"{file_path} dosyasına yazılırken hata oluştu: {e}")

def get_user_data_path(username, data_type='history'):
    """Kullanıcıya özel JSON dosyalarının yolunu döner."""
    user_dir = os.path.join(USERS_DIR, username)
    os.makedirs(user_dir, exist_ok=True)
    if data_type == 'history':
        return os.path.join(user_dir, f'{username}.json')
    elif data_type == 'likes_playlists':
        return os.path.join(user_dir, f'{username}likedandplaylist.json')
    return None

# --- Oturum Yönetimi ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- Service.py Mantığı (Şarkı Verilerini Çekme ve Yönetme) ---

def get_stream_url(video_id):
    """yt-dlp kullanarak bir video ID'si için ses akış URL'sini alır."""
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True, # Sadece URL'yi hızlıca almak için
        'force_generic_extractor': True
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            # Daha detaylı bilgi alarak en iyi ses formatını seçelim
            info = ydl.extract_info(info['url'], download=False)
            best_audio_format = None
            for f in info.get('formats', []):
                if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                    if best_audio_format is None or f.get('abr', 0) > best_audio_format.get('abr', 0):
                        best_audio_format = f
            
            if best_audio_format:
                 logging.info(f"{video_id} için stream URL'si başarıyla alındı.")
                 return best_audio_format['url']
            else:
                 # Ses formatı bulunamazsa ilk URL'yi dene
                 logging.warning(f"{video_id} için sadece ses formatı bulunamadı, ilk URL deneniyor.")
                 return info['formats'][0]['url']
    except Exception as e:
        logging.error(f"{video_id} için stream URL'si alınırken hata: {e}")
        return None

def fetch_and_save_artist_tracks(artist_name):
    """Bir sanatçının tüm şarkılarını YT Music'ten alıp links.json'a kaydeder."""
    if not ytmusic:
        logging.error("YTMusic API kullanılamıyor.")
        return
        
    logging.info(f"'{artist_name}' için şarkılar çekiliyor...")
    try:
        search_results = ytmusic.search(artist_name, filter="artists")
        if not search_results:
            logging.warning(f"'{artist_name}' adında bir sanatçı bulunamadı.")
            return

        artist_id = search_results[0]['browseId']
        artist_details = ytmusic.get_artist(artist_id)
        
        links_data = read_json(LINKS_FILE, [])
        existing_ids = {track['videoId'] for track in links_data}

        tracks_to_add = []
        if 'songs' in artist_details and artist_details['songs'] and 'results' in artist_details['songs']:
            for track in artist_details['songs']['results']:
                if track['videoId'] not in existing_ids:
                    thumbnail_url = track['thumbnails'][-1]['url'].replace('w120-h120', 'w544-h544')
                    track_data = {
                        'videoId': track['videoId'],
                        'title': track['title'],
                        'artists': [artist['name'] for artist in track.get('artists', [])],
                        'album': track.get('album', {}).get('name') if track.get('album') else "Single",
                        'duration': track.get('duration'),
                        'thumbnail': thumbnail_url,
                        'last_updated': datetime.now().isoformat()
                    }
                    tracks_to_add.append(track_data)
                    existing_ids.add(track['videoId'])
        
        if tracks_to_add:
            links_data.extend(tracks_to_add)
            write_json(LINKS_FILE, links_data)
            logging.info(f"'{artist_name}' için {len(tracks_to_add)} yeni şarkı eklendi.")
        else:
            logging.info(f"'{artist_name}' için yeni şarkı bulunamadı.")

    except Exception as e:
        logging.error(f"'{artist_name}' için şarkılar çekilirken bir hata oluştu: {e}")


def background_track_updater():
    """Arka planda sanatçı listesini periyodik olarak günceller."""
    while True:
        try:
            admin_config = read_json(ADMIN_CONFIG_FILE, {"artists": []})
            artists = admin_config.get("artists", [])
            logging.info(f"Otomatik güncelleme başlıyor. Sanatçılar: {artists}")
            for artist_name in artists:
                fetch_and_save_artist_tracks(artist_name)
                time.sleep(5) # API'ye çok yüklenmemek için kısa bir bekleme
            logging.info("Otomatik güncelleme tamamlandı. 3 saat bekleniyor.")
        except Exception as e:
            logging.error(f"Arka plan güncelleyicide hata: {e}")
        time.sleep(3 * 60 * 60) # 3 saat bekle


# --- Algorithm.py Mantığı (Öneri Sistemi) ---

def get_recommendations(username, limit=20):
    """Kullanıcının dinleme geçmişine ve beğenilerine göre öneriler oluşturur."""
    history_path = get_user_data_path(username, 'history')
    likes_path = get_user_data_path(username, 'likes_playlists')

    history = read_json(history_path, [])
    likes_data = read_json(likes_path, {"liked_songs": [], "playlists": []})
    liked_song_ids = set(likes_data.get("liked_songs", []))

    if not history and not liked_song_ids:
        # Kullanıcı verisi yoksa popüler şarkılardan dön
        popular = read_json(POPULAR_FILE, [])
        return popular[:limit]

    # Sanatçı ve tür skorlaması
    artist_scores = {}
    for entry in history:
        song_details = get_song_details(entry['videoId'])
        if song_details:
            for artist in song_details.get('artists', []):
                artist_scores[artist] = artist_scores.get(artist, 0) + 1

    for videoId in liked_song_ids:
        song_details = get_song_details(videoId)
        if song_details:
            for artist in song_details.get('artists', []):
                artist_scores[artist] = artist_scores.get(artist, 0) + 5 # Beğenilen şarkının sanatçısına daha fazla puan

    if not artist_scores:
        popular = read_json(POPULAR_FILE, [])
        return popular[:limit]

    sorted_artists = sorted(artist_scores.items(), key=lambda item: item[1], reverse=True)
    top_artists = [artist for artist, score in sorted_artists[:5]]

    # Önerileri bul
    recommendations = []
    all_songs = read_json(LINKS_FILE, [])
    listened_ids = {entry['videoId'] for entry in history}

    for song in all_songs:
        if song['videoId'] not in listened_ids and song['videoId'] not in liked_song_ids:
            # En çok dinlenen sanatçıların diğer şarkılarını öner
            if any(artist in song.get('artists', []) for artist in top_artists):
                recommendations.append(song)
    
    # Yeterli öneri yoksa popülerlerden ekle
    if len(recommendations) < limit:
        popular = read_json(POPULAR_FILE, [])
        for song in popular:
            if len(recommendations) >= limit:
                break
            if song['videoId'] not in listened_ids and song['videoId'] not in liked_song_ids:
                if song not in recommendations:
                    recommendations.append(song)

    return recommendations[:limit]


def update_popular_tracks():
    """Global popüler şarkılar listesini günceller."""
    if not ytmusic: return
    try:
        logging.info("Popüler şarkılar listesi güncelleniyor...")
        playlist = ytmusic.get_playlist("PL4fGSI1pDJn6jG6g4n2e4aYy8rAlv5I_s") # YouTube Music Global Top 100
        
        popular_tracks = []
        for track in playlist['tracks'][:50]:
             thumbnail_url = track['thumbnails'][-1]['url'].replace('w120-h120', 'w544-h544')
             track_data = {
                'videoId': track['videoId'],
                'title': track['title'],
                'artists': [artist['name'] for artist in track.get('artists', [])],
                'album': track.get('album', {}).get('name') if track.get('album') else "Single",
                'duration': track.get('duration'),
                'thumbnail': thumbnail_url
             }
             popular_tracks.append(track_data)
        
        write_json(POPULAR_FILE, popular_tracks)
        logging.info("Popüler şarkılar listesi başarıyla güncellendi.")
    except Exception as e:
        logging.error(f"Popüler şarkılar güncellenirken hata: {e}")

# --- API Endpoint'leri ---

@app.route('/api/search')
@login_required
def api_search():
    query = request.args.get('q', '')
    if not query or not ytmusic:
        return jsonify([])

    try:
        search_results = ytmusic.search(query, filter="songs", limit=20)
        results = []
        for item in search_results:
            thumbnail_url = item['thumbnails'][-1]['url'].replace('w120-h120', 'w544-h544')
            results.append({
                'videoId': item['videoId'],
                'title': item['title'],
                'artists': [artist['name'] for artist in item.get('artists', [])],
                'album': item.get('album', {}).get('name') if item.get('album') else "Single",
                'duration': item.get('duration'),
                'thumbnail': thumbnail_url
            })
        return jsonify(results)
    except Exception as e:
        logging.error(f"Arama sırasında hata: {e}")
        return jsonify({"error": "Arama sırasında bir hata oluştu"}), 500

@app.route('/stream/<video_id>')
@login_required
def stream_audio(video_id):
    url = get_stream_url(video_id)
    if url:
        return redirect(url)
    return Response("Stream URL alınamadı.", status=500)

def get_song_details(video_id):
    """Verilen videoId için şarkı detaylarını links.json'dan bulur."""
    all_songs = read_json(LINKS_FILE, [])
    for song in all_songs:
        if song['videoId'] == video_id:
            return song
    # links.json'da yoksa popüler listede ara
    popular_songs = read_json(POPULAR_FILE, [])
    for song in popular_songs:
        if song['videoId'] == video_id:
            return song
            
    # Hiçbir yerde yoksa API'den çekmeyi dene (daha yavaş)
    if ytmusic:
        try:
            song_data = ytmusic.get_song(video_id)
            track = song_data['videoDetails']
            thumbnail_url = track['thumbnail']['thumbnails'][-1]['url'].replace('w120-h120', 'w544-h544')
            return {
                'videoId': track['videoId'],
                'title': track['title'],
                'artists': [track['author']],
                'album': "Bilinmiyor",
                'duration': track.get('lengthSeconds'),
                'thumbnail': thumbnail_url
            }
        except Exception:
            return None # Hata durumunda veya bulunamazsa
    return None

@app.route('/api/song_details/<video_id>')
@login_required
def api_song_details(video_id):
    details = get_song_details(video_id)
    if details:
        return jsonify(details)
    return jsonify({"error": "Şarkı bulunamadı"}), 404


@app.route('/api/home_data')
@login_required
def api_home_data():
    username = session['username']
    recommendations = get_recommendations(username, limit=20)
    popular = read_json(POPULAR_FILE, [])[:20]
    return jsonify({
        "recommendations": recommendations,
        "popular": popular
    })

@app.route('/api/library_data')
@login_required
def api_library_data():
    username = session['username']
    likes_path = get_user_data_path(username, 'likes_playlists')
    data = read_json(likes_path, {"liked_songs": [], "playlists": []})
    
    liked_songs_details = [get_song_details(vid) for vid in data.get("liked_songs", [])]
    liked_songs_details = [s for s in liked_songs_details if s is not None]

    playlists_with_covers = []
    for p in data.get("playlists", []):
        cover_url = "https://placehold.co/544x544/121212/FFFFFF?text=+"
        if p.get("songs"):
            first_song_details = get_song_details(p["songs"][0])
            if first_song_details:
                cover_url = first_song_details['thumbnail']
        p['cover'] = cover_url
        playlists_with_covers.append(p)

    return jsonify({
        "liked_songs": liked_songs_details,
        "playlists": playlists_with_covers
    })
    
@app.route('/api/playlist/<playlist_id>')
@login_required
def api_get_playlist(playlist_id):
    username = session['username']
    likes_path = get_user_data_path(username, 'likes_playlists')
    data = read_json(likes_path, {"liked_songs": [], "playlists": []})
    
    playlist_found = None
    for p in data.get("playlists", []):
        if p.get("id") == playlist_id:
            playlist_found = p
            break
            
    if not playlist_found:
        return jsonify({"error": "Çalma listesi bulunamadı"}), 404
        
    song_details = [get_song_details(vid) for vid in playlist_found.get("songs", [])]
    song_details = [s for s in song_details if s is not None]
    playlist_found["songs_details"] = song_details
    
    return jsonify(playlist_found)


@app.route('/api/log_play', methods=['POST'])
@login_required
def log_play():
    data = request.json
    video_id = data.get('videoId')
    if not video_id:
        return jsonify({"error": "videoId gerekli"}), 400

    username = session['username']
    history_path = get_user_data_path(username, 'history')
    history = read_json(history_path, [])
    
    history.append({
        "videoId": video_id,
        "timestamp": datetime.now().isoformat()
    })
    
    # Geçmişi çok büyütmemek için son 500 şarkıyı tut
    write_json(history_path, history[-500:])
    return jsonify({"success": True})


@app.route('/api/toggle_like', methods=['POST'])
@login_required
def toggle_like():
    data = request.json
    video_id = data.get('videoId')
    if not video_id:
        return jsonify({"error": "videoId gerekli"}), 400

    username = session['username']
    likes_path = get_user_data_path(username, 'likes_playlists')
    likes_data = read_json(likes_path, {"liked_songs": [], "playlists": []})
    
    liked_songs = set(likes_data.get("liked_songs", []))
    is_liked = False
    if video_id in liked_songs:
        liked_songs.remove(video_id)
        is_liked = False
    else:
        liked_songs.add(video_id)
        is_liked = True
    
    likes_data["liked_songs"] = list(liked_songs)
    write_json(likes_path, likes_data)
    
    return jsonify({"success": True, "is_liked": is_liked})

@app.route('/api/check_like_status/<video_id>')
@login_required
def check_like_status(video_id):
    username = session['username']
    likes_path = get_user_data_path(username, 'likes_playlists')
    likes_data = read_json(likes_path, {"liked_songs": [], "playlists": []})
    is_liked = video_id in likes_data.get("liked_songs", [])
    return jsonify({"is_liked": is_liked})
    
@app.route('/api/playlists/create', methods=['POST'])
@login_required
def create_playlist():
    data = request.json
    playlist_name = data.get('name')
    if not playlist_name:
        return jsonify({"error": "Çalma listesi adı gerekli"}), 400

    username = session['username']
    likes_path = get_user_data_path(username, 'likes_playlists')
    data = read_json(likes_path, {"liked_songs": [], "playlists": []})
    
    new_playlist = {
        "id": f"pl_{int(time.time())}",
        "name": playlist_name,
        "songs": []
    }
    data["playlists"].append(new_playlist)
    write_json(likes_path, data)
    
    return jsonify(new_playlist), 201

@app.route('/api/playlists/add_song', methods=['POST'])
@login_required
def add_song_to_playlist():
    data = request.json
    playlist_id = data.get('playlistId')
    video_id = data.get('videoId')
    if not playlist_id or not video_id:
        return jsonify({"error": "playlistId and videoId are required"}), 400
        
    username = session['username']
    likes_path = get_user_data_path(username, 'likes_playlists')
    user_data = read_json(likes_path)

    playlist_found = False
    for p in user_data.get("playlists", []):
        if p.get("id") == playlist_id:
            if video_id not in p["songs"]:
                p["songs"].append(video_id)
            playlist_found = True
            break
            
    if not playlist_found:
        return jsonify({"error": "Playlist not found"}), 404

    write_json(likes_path, user_data)
    return jsonify({"success": True})


# --- Admin Paneli ---
@app.route('/admin', methods=['GET', 'POST'])
@login_required
def admin_panel():
    # Basit bir admin kontrolü, ilk kayıt olan kullanıcı admin olsun
    passwords = read_json(PASSWORDS_FILE, {})
    if not passwords or list(passwords.keys())[0] != session['username']:
        return "Yetkiniz yok.", 403

    if request.method == 'POST':
        artist_name = request.form.get('artist_name')
        if artist_name:
            # Arka planda çalıştır
            threading.Thread(target=fetch_and_save_artist_tracks, args=(artist_name,)).start()
            
            # Sanatçıyı kalıcı listeye ekle
            admin_config = read_json(ADMIN_CONFIG_FILE, {"artists": []})
            if artist_name not in admin_config['artists']:
                admin_config['artists'].append(artist_name)
                write_json(ADMIN_CONFIG_FILE, admin_config)

            return redirect(url_for('admin_panel'))
            
    admin_config = read_json(ADMIN_CONFIG_FILE, {"artists": []})
    artists = admin_config.get("artists", [])
    return render_template_string("""
        <!DOCTYPE html>
        <html lang="tr">
        <head>
            <meta charset="UTF-8">
            <title>Admin Paneli</title>
            <style>
                body { font-family: sans-serif; background: #121212; color: #fff; padding: 20px; }
                h1 { color: #1DB954; }
                form { margin-bottom: 20px; }
                input[type=text] { padding: 10px; width: 300px; }
                input[type=submit] { padding: 10px 20px; background: #1DB954; color: #fff; border: none; cursor: pointer; }
                ul { list-style: none; padding: 0; }
                li { background: #282828; padding: 10px; margin-bottom: 5px; }
            </style>
        </head>
        <body>
            <h1>Admin Paneli - Sanatçı Ekle</h1>
            <p>Buradan eklenen sanatçılar 3 saatte bir otomatik olarak güncellenecektir.</p>
            <form method="post">
                <input type="text" name="artist_name" placeholder="Örn: The Weeknd" required>
                <input type="submit" value="Sanatçıyı Çek ve Ekle">
            </form>
            <h2>Takip Edilen Sanatçılar</h2>
            <ul>
                {% for artist in artists %}
                    <li>{{ artist }}</li>
                {% endfor %}
            </ul>
        </body>
        </html>
    """, artists=artists)

# --- HTML Sayfaları ve Arayüz ---
# Tüm HTML, CSS ve JS tek bir ana şablonda birleştirilmiştir.

MAIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="tr" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Müzik Platformu</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://kit.fontawesome.com/a076d05399.js" crossorigin="anonymous"></script>
     <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body { font-family: 'Inter', sans-serif; background-color: #000; color: #fff; }
        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: #121212; }
        ::-webkit-scrollbar-thumb { background: #535353; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: #737373; }
        .main-view { background-color: #121212; }
        .song-card:hover .play-button { opacity: 1; transform: translateY(0); }
        .play-button { opacity: 0; transform: translateY(10px); transition: opacity 0.3s, transform 0.3s; }
        input[type="range"]::-webkit-slider-thumb {
            -webkit-appearance: none;
            appearance: none;
            width: 12px;
            height: 12px;
            background: #fff;
            border-radius: 50%;
            cursor: pointer;
            margin-top: -4px;
            opacity: 0;
            transition: opacity 0.2s;
        }
        input[type="range"]:hover::-webkit-slider-thumb {
            opacity: 1;
        }
        .sidebar a.active, .bottom-nav a.active { color: #fff; }
    </style>
</head>
<body class="text-gray-300">
    <div class="h-screen w-screen flex flex-col">
        <div class="flex flex-1 overflow-hidden">
            <!-- Yan Panel (Masaüstü) -->
            <aside class="hidden md:flex flex-col w-64 bg-black p-2 space-y-2">
                <nav class="bg-[#121212] rounded-lg p-2">
                    <ul class="space-y-2">
                        <li><a href="#" onclick="loadPage('index')" class="nav-link flex items-center gap-4 text-gray-400 font-bold hover:text-white transition-colors duration-200 p-2 rounded-md"><i class="fas fa-home w-6 text-center"></i> Anasayfa</a></li>
                        <li><a href="#" onclick="loadPage('search')" class="nav-link flex items-center gap-4 text-gray-400 font-bold hover:text-white transition-colors duration-200 p-2 rounded-md"><i class="fas fa-search w-6 text-center"></i> Ara</a></li>
                    </ul>
                </nav>
                <div class="bg-[#121212] rounded-lg p-2 flex-1">
                    <a href="#" onclick="loadPage('library')" class="nav-link flex items-center gap-4 text-gray-400 font-bold hover:text-white transition-colors duration-200 p-2 rounded-md"><i class="fas fa-book w-6 text-center"></i> Kitaplığın</a>
                    <div id="sidebar-playlists" class="mt-4 space-y-2 overflow-y-auto max-h-[calc(100vh-350px)]">
                        <!-- Playlistler buraya gelecek -->
                    </div>
                </div>
            </aside>
            
            <!-- Ana İçerik -->
            <main id="main-content" class="flex-1 main-view overflow-y-auto p-4 md:p-6 rounded-lg m-0 md:m-2 md:ml-0">
                <!-- Sayfa içeriği buraya dinamik olarak yüklenecek -->
            </main>
        </div>

        <!-- Oynatıcı Paneli (Alt) -->
        <footer id="player-bar" class="bg-black border-t border-gray-800 p-3 flex items-center justify-between gap-4">
            <div class="w-1/4 flex items-center gap-3">
                <img id="player-thumbnail" src="https://placehold.co/64x64/121212/FFFFFF?text=M" alt="Albüm Kapağı" class="w-14 h-14 rounded-md">
                <div>
                    <h3 id="player-title" class="font-semibold text-white">Şarkı Seçilmedi</h3>
                    <p id="player-artist" class="text-xs text-gray-400"></p>
                </div>
                <button id="player-like-btn" class="text-gray-400 hover:text-white ml-4 hidden"><i class="far fa-heart"></i></button>
            </div>
            <div class="w-1/2 flex flex-col items-center justify-center">
                <div class="flex items-center gap-4 text-lg">
                    <button class="text-gray-400 hover:text-white"><i class="fas fa-random"></i></button>
                    <button class="text-gray-400 hover:text-white"><i class="fas fa-step-backward"></i></button>
                    <button id="player-play-pause" class="bg-white text-black rounded-full w-8 h-8 flex items-center justify-center text-sm hover:scale-105"><i class="fas fa-play"></i></button>
                    <button class="text-gray-400 hover:text-white"><i class="fas fa-step-forward"></i></button>
                    <button class="text-gray-400 hover:text-white"><i class="fas fa-redo"></i></button>
                </div>
                <div class="w-full flex items-center gap-2 mt-2 text-xs">
                    <span id="current-time">0:00</span>
                    <input id="progress-bar" type="range" min="0" max="100" value="0" class="w-full h-1 bg-gray-700 rounded-lg appearance-none cursor-pointer">
                    <span id="total-time">0:00</span>
                </div>
            </div>
            <div class="w-1/4 flex items-center justify-end gap-2">
                <button onclick="loadPage('player')" class="text-gray-400 hover:text-white"><i class="fa-solid fa-up-right-and-down-left-from-center"></i></button>
                <button class="text-gray-400 hover:text-white"><i class="fas fa-list-ul"></i></button>
                <i class="fas fa-volume-down text-gray-400"></i>
                <input id="volume-bar" type="range" min="0" max="100" value="50" class="w-24 h-1 bg-gray-700 rounded-lg appearance-none cursor-pointer">
            </div>
        </footer>

        <!-- Alt Navigasyon (Mobil) -->
        <nav class="md:hidden bg-[#121212] fixed bottom-0 left-0 right-0 p-2 flex justify-around border-t border-gray-800" style="padding-bottom: env(safe-area-inset-bottom);">
             <a href="#" onclick="loadPage('index')" class="nav-link flex flex-col items-center text-gray-400 hover:text-white space-y-1">
                <i class="fas fa-home"></i>
                <span class="text-xs">Anasayfa</span>
            </a>
            <a href="#" onclick="loadPage('search')" class="nav-link flex flex-col items-center text-gray-400 hover:text-white space-y-1">
                <i class="fas fa-search"></i>
                <span class="text-xs">Ara</span>
            </a>
            <a href="#" onclick="loadPage('library')" class="nav-link flex flex-col items-center text-gray-400 hover:text-white space-y-1">
                <i class="fas fa-book"></i>
                <span class="text-xs">Kitaplığın</span>
            </a>
        </nav>
    </div>
    
    <div id="modal-backdrop" class="fixed inset-0 bg-black bg-opacity-70 hidden flex items-center justify-center z-50">
        <div id="playlist-modal" class="bg-[#282828] p-6 rounded-lg w-full max-w-sm">
             <h2 class="text-xl font-bold mb-4">Çalma Listesine Ekle</h2>
             <ul id="modal-playlist-list" class="max-h-60 overflow-y-auto mb-4">
                <!-- Çalma listeleri buraya eklenecek -->
             </ul>
             <div class="flex justify-end gap-2">
                <button onclick="closePlaylistModal()" class="bg-gray-500 hover:bg-gray-600 text-white font-bold py-2 px-4 rounded">Kapat</button>
             </div>
        </div>
    </div>


    <audio id="audio-player"></audio>

    <script>
        const audioPlayer = document.getElementById('audio-player');
        let currentTrack = null;
        let currentQueue = [];
        let currentQueueIndex = -1;

        // Player UI Elements
        const playerBar = document.getElementById('player-bar');
        const playerThumbnail = document.getElementById('player-thumbnail');
        const playerTitle = document.getElementById('player-title');
        const playerArtist = document.getElementById('player-artist');
        const playerPlayPause = document.getElementById('player-play-pause');
        const progressBar = document.getElementById('progress-bar');
        const currentTimeEl = document.getElementById('current-time');
        const totalTimeEl = document.getElementById('total-time');
        const volumeBar = document.getElementById('volume-bar');
        const playerLikeBtn = document.getElementById('player-like-btn');
        
        // --- Dinamik Sayfa Yükleme ---
        async function loadPage(pageName, params = {}) {
            console.log(`Loading page: ${pageName}`);
            const mainContent = document.getElementById('main-content');
            mainContent.innerHTML = '<div class="flex justify-center items-center h-full"><i class="fas fa-spinner fa-spin text-4xl"></i></div>';
            
            try {
                let htmlContent = '';
                if (pageName === 'index') {
                    htmlContent = await getIndexContent();
                } else if (pageName === 'search') {
                    htmlContent = getSearchContent();
                } else if (pageName === 'library') {
                    htmlContent = await getLibraryContent();
                } else if (pageName === 'player') {
                    htmlContent = getPlayerContent();
                } else if (pageName === 'playlist') {
                    htmlContent = await getPlaylistContent(params.id);
                }
                mainContent.innerHTML = htmlContent;
                
                // Sayfa yüklendikten sonra gerekli JS fonksiyonlarını çalıştır
                if (pageName === 'search') {
                    document.getElementById('search-input').addEventListener('keyup', performSearch);
                }

                updateActiveNavLink(pageName);
            } catch (error) {
                console.error('Page load error:', error);
                mainContent.innerHTML = '<p class="text-red-500">Sayfa yüklenirken bir hata oluştu.</p>';
            }
        }
        
        function updateActiveNavLink(pageName) {
            document.querySelectorAll('.nav-link').forEach(link => {
                link.classList.remove('active');
                if (link.getAttribute('onclick').includes(`loadPage('${pageName}')`)) {
                    link.classList.add('active');
                }
            });
        }


        // --- İçerik Getirme Fonksiyonları ---
        async function getIndexContent() {
            const response = await fetch('/api/home_data');
            const data = await response.json();
            
            const recommendationsHtml = data.recommendations.length > 0 
                ? generateSongSection('Sana Özel', data.recommendations) 
                : '<p>Henüz senin için bir önerimiz yok. Biraz müzik dinle!</p>';
            
            const popularHtml = generateSongSection('Popüler', data.popular);

            return `
                <h1 class="text-3xl font-bold mb-6">Anasayfa</h1>
                ${recommendationsHtml}
                ${popularHtml}
            `;
        }

        function getSearchContent() {
            return `
                <div class="sticky top-0 bg-[#121212] pt-4 pb-2 z-10">
                     <div class="relative">
                        <i class="fas fa-search absolute left-4 top-1/2 -translate-y-1/2 text-gray-400"></i>
                        <input id="search-input" type="text" placeholder="Ne dinlemek istersin?" class="w-full bg-[#2a2a2a] text-white rounded-full py-3 pl-12 pr-4 focus:outline-none focus:ring-2 focus:ring-green-500">
                    </div>
                </div>
                <div id="search-results" class="mt-6 grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4">
                    <!-- Arama sonuçları buraya gelecek -->
                </div>
            `;
        }
        
        async function getLibraryContent() {
            const response = await fetch('/api/library_data');
            const data = await response.json();

            const likedSongsHtml = data.liked_songs.length > 0
                ? generateSongSection('Beğenilen Şarkılar', data.liked_songs)
                : '<p>Henüz şarkı beğenmedin.</p>';
            
            let playlistsHtml = '<div class="mb-8"><button onclick="showCreatePlaylistPrompt()" class="bg-green-600 text-white font-bold py-2 px-4 rounded-full hover:bg-green-700">+ Yeni Çalma Listesi</button></div>'
            playlistsHtml += generatePlaylistSection('Çalma Listeleri', data.playlists);

            return `
                <h1 class="text-3xl font-bold mb-6">Kitaplığın</h1>
                ${playlistsHtml}
                ${likedSongsHtml}
            `;
        }
        
        async function getPlaylistContent(playlistId) {
            const response = await fetch('/api/playlist/' + playlistId);
            if (!response.ok) return '<p>Çalma listesi bulunamadı.</p>';
            const playlist = await response.json();

            let songsHtml = '<ol class="mt-4 space-y-2">';
            playlist.songs_details.forEach((song, index) => {
                songsHtml += `
                    <li class="flex items-center p-2 rounded-md hover:bg-white/10 cursor-pointer" onclick="playSongFromPlaylist('${playlistId}', ${index})">
                        <span class="w-8 text-gray-400">${index + 1}</span>
                        <img src="${song.thumbnail}" class="w-10 h-10 rounded-md mr-4">
                        <div class="flex-grow">
                            <p class="text-white font-semibold">${song.title}</p>
                            <p class="text-sm text-gray-400">${song.artists.join(', ')}</p>
                        </div>
                        <span class="text-sm text-gray-400">${song.duration || ''}</span>
                    </li>
                `;
            });
            songsHtml += '</ol>';
            
            const coverUrl = playlist.songs_details.length > 0 ? playlist.songs_details[0].thumbnail : "https://placehold.co/150x150/121212/FFFFFF?text=+"

            return `
                <div class="flex items-end gap-6 mb-8">
                    <img src="${coverUrl}" class="w-36 h-36 md:w-48 md:h-48 rounded-lg shadow-lg">
                    <div>
                        <p class="text-sm">ÇALMA LİSTESİ</p>
                        <h1 class="text-4xl md:text-6xl font-extrabold">${playlist.name}</h1>
                        <p class="mt-4 text-gray-300">${playlist.songs.length} şarkı</p>
                    </div>
                </div>
                ${songsHtml}
            `;
        }

        function getPlayerContent() {
            if (!currentTrack) {
                return '<div class="h-full flex flex-col items-center justify-center text-center"><h2 class="text-2xl font-bold">Henüz bir şarkı çalmıyor.</h2><p class="text-gray-400 mt-2">Dinlemek için bir şarkı seç.</p></div>';
            }
            const { thumbnail, title, artists } = currentTrack;
            return `
                <div class="h-full flex flex-col items-center justify-center p-4 text-center">
                    <img src="${thumbnail}" class="w-64 h-64 md:w-96 md:h-96 rounded-lg shadow-2xl mb-8">
                    <h2 class="text-3xl font-bold">${title}</h2>
                    <p class="text-lg text-gray-400 mt-2">${artists.join(', ')}</p>
                    
                     <div class="w-full max-w-md flex flex-col items-center justify-center mt-8">
                        <div class="w-full flex items-center gap-2 mt-2 text-xs">
                             <span id="player-view-current-time">0:00</span>
                             <input id="player-view-progress-bar" type="range" min="0" max="100" value="0" class="w-full h-1 bg-gray-700 rounded-lg appearance-none cursor-pointer">
                             <span id="player-view-total-time">0:00</span>
                        </div>
                        <div class="flex items-center gap-6 text-2xl mt-4">
                            <button class="text-gray-400 hover:text-white"><i class="fas fa-step-backward"></i></button>
                            <button id="player-view-play-pause" class="bg-white text-black rounded-full w-14 h-14 flex items-center justify-center text-xl hover:scale-105"><i class="fas fa-play"></i></button>
                            <button class="text-gray-400 hover:text-white"><i class="fas fa-step-forward"></i></button>
                        </div>
                     </div>
                </div>
            `;
        }
        
        async function showCreatePlaylistPrompt() {
            const name = prompt("Yeni çalma listesi adı:");
            if (name) {
                await fetch('/api/playlists/create', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name})
                });
                loadPage('library'); // Sayfayı ve kenar çubuğunu yenile
                updateSidebarPlaylists();
            }
        }
        
        async function updateSidebarPlaylists() {
            const response = await fetch('/api/library_data');
            const data = await response.json();
            const sidebar = document.getElementById('sidebar-playlists');
            sidebar.innerHTML = '';
            data.playlists.forEach(p => {
                const a = document.createElement('a');
                a.href = '#';
                a.className = 'block text-gray-400 hover:text-white text-sm p-1 rounded-md';
                a.textContent = p.name;
                a.onclick = () => loadPage('playlist', {id: p.id});
                sidebar.appendChild(a);
            });
        }


        // --- HTML Üretim Yardımcıları ---
        function generateSongSection(title, songs) {
            let cardsHtml = songs.map(song => generateSongCard(song)).join('');
            return `
                <section class="mb-8">
                    <h2 class="text-2xl font-bold mb-4">${title}</h2>
                    <div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4">
                        ${cardsHtml}
                    </div>
                </section>
            `;
        }

        function generateSongCard(song) {
            return `
                <div class="song-card bg-[#181818] p-4 rounded-lg hover:bg-[#282828] transition-colors duration-300 group cursor-pointer" onclick="playSong('${song.videoId}')">
                    <div class="relative">
                        <img src="${song.thumbnail}" alt="${song.title}" class="w-full h-auto rounded-md shadow-lg mb-4">
                        <button class="play-button absolute bottom-2 right-2 bg-green-500 text-black rounded-full w-12 h-12 flex items-center justify-center shadow-lg">
                            <i class="fas fa-play text-xl"></i>
                        </button>
                    </div>
                    <h3 class="font-bold text-white truncate">${song.title}</h3>
                    <p class="text-sm text-gray-400 truncate">${song.artists.join(', ')}</p>
                </div>
            `;
        }
        
        function generatePlaylistSection(title, playlists) {
            let cardsHtml = playlists.map(p => `
                <div class="bg-[#181818] p-4 rounded-lg hover:bg-[#282828] transition-colors duration-300 group cursor-pointer" onclick="loadPage('playlist', {id: '${p.id}'})">
                     <img src="${p.cover}" alt="${p.name}" class="w-full h-auto rounded-md shadow-lg mb-4">
                     <h3 class="font-bold text-white truncate">${p.name}</h3>
                     <p class="text-sm text-gray-400">${p.songs.length} şarkı</p>
                </div>
            `).join('');
            
            return `
                 <section class="mb-8">
                    <h2 class="text-2xl font-bold mb-4">${title}</h2>
                    <div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4">
                        ${cardsHtml}
                    </div>
                </section>
            `;
        }

        // --- Arama ---
        let searchTimeout;
        function performSearch() {
            clearTimeout(searchTimeout);
            const query = document.getElementById('search-input').value;
            if (query.length < 2) {
                 document.getElementById('search-results').innerHTML = '';
                 return
            };

            searchTimeout = setTimeout(async () => {
                const response = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
                const results = await response.json();
                const resultsContainer = document.getElementById('search-results');
                resultsContainer.innerHTML = results.map(song => generateSongCard(song)).join('');
            }, 300);
        }

        // --- Müzik Çalar Mantığı ---
        async function playSong(videoId, queue = null) {
            try {
                const response = await fetch(`/api/song_details/${videoId}`);
                if (!response.ok) throw new Error('Şarkı detayları alınamadı');
                const songDetails = await response.json();
                
                currentTrack = songDetails;
                audioPlayer.src = `/stream/${videoId}`;
                audioPlayer.play();
                
                if (queue) {
                    currentQueue = queue;
                    currentQueueIndex = currentQueue.findIndex(s => s.videoId === videoId);
                } else {
                    currentQueue = [songDetails];
                    currentQueueIndex = 0;
                }
                
                updatePlayerUI(songDetails);
                checkLikeStatus(videoId);
                
                fetch('/api/log_play', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({videoId: videoId})
                });

            } catch (error) {
                console.error("Şarkı çalınırken hata:", error);
                playerTitle.textContent = "Şarkı Yüklenemedi";
            }
        }
        
        async function playSongFromPlaylist(playlistId, songIndex) {
            const res = await fetch('/api/playlist/' + playlistId);
            const playlist = await res.json();
            const songQueue = playlist.songs_details;
            const videoId = songQueue[songIndex].videoId;
            playSong(videoId, songQueue);
        }
        
        function updatePlayerUI(song) {
            playerThumbnail.src = song.thumbnail;
            playerTitle.textContent = song.title;
            playerArtist.textContent = song.artists.join(', ');
            playerPlayPause.innerHTML = '<i class="fas fa-pause"></i>';
            playerBar.classList.remove('hidden');
            playerLikeBtn.classList.remove('hidden');

            // Eğer tam ekran oynatıcı açıksa onu da güncelle
             if (document.getElementById('player-view-play-pause')) {
                loadPage('player'); // refresh the view
            }
        }
        
        function formatTime(seconds) {
            const minutes = Math.floor(seconds / 60);
            const secs = Math.floor(seconds % 60);
            return `${minutes}:${secs < 10 ? '0' : ''}${secs}`;
        }

        audioPlayer.addEventListener('timeupdate', () => {
            const { currentTime, duration } = audioPlayer;
            if (duration) {
                const progressPercent = (currentTime / duration) * 100;
                progressBar.value = progressPercent;
                currentTimeEl.textContent = formatTime(currentTime);
                // Tam ekran oynatıcı
                if(document.getElementById('player-view-progress-bar')) {
                    document.getElementById('player-view-progress-bar').value = progressPercent;
                    document.getElementById('player-view-current-time').textContent = formatTime(currentTime);
                }
            }
        });
        
        audioPlayer.addEventListener('loadedmetadata', () => {
            totalTimeEl.textContent = formatTime(audioPlayer.duration);
             if(document.getElementById('player-view-total-time')) {
                document.getElementById('player-view-total-time').textContent = formatTime(audioPlayer.duration);
            }
        });

        progressBar.addEventListener('input', () => {
            const { duration } = audioPlayer;
            audioPlayer.currentTime = (progressBar.value / 100) * duration;
        });

        playerPlayPause.addEventListener('click', () => {
            if (audioPlayer.paused) {
                audioPlayer.play();
            } else {
                audioPlayer.pause();
            }
        });
        
        document.body.addEventListener('click', e => {
            const playPauseBtn = e.target.closest('#player-view-play-pause');
            if (playPauseBtn) {
                 if (audioPlayer.paused) audioPlayer.play();
                 else audioPlayer.pause();
            }
        });

        audioPlayer.addEventListener('play', () => {
            playerPlayPause.innerHTML = '<i class="fas fa-pause"></i>';
             if(document.getElementById('player-view-play-pause')) {
                document.getElementById('player-view-play-pause').innerHTML = '<i class="fas fa-pause"></i>';
            }
        });

        audioPlayer.addEventListener('pause', () => {
            playerPlayPause.innerHTML = '<i class="fas fa-play"></i>';
             if(document.getElementById('player-view-play-pause')) {
                document.getElementById('player-view-play-pause').innerHTML = '<i class="fas fa-play"></i>';
            }
        });
        
        volumeBar.addEventListener('input', (e) => {
            audioPlayer.volume = e.target.value / 100;
        });
        
        // --- Beğenme ve Çalma Listesi ---
        playerLikeBtn.addEventListener('click', async () => {
             if (!currentTrack) return;
             const response = await fetch('/api/toggle_like', {
                 method: 'POST',
                 headers: {'Content-Type': 'application/json'},
                 body: JSON.stringify({videoId: currentTrack.videoId})
             });
             const data = await response.json();
             updateLikeButton(data.is_liked);
        });
        
        async function checkLikeStatus(videoId) {
            const response = await fetch(`/api/check_like_status/${videoId}`);
            const data = await response.json();
            updateLikeButton(data.is_liked);
        }

        function updateLikeButton(isLiked) {
            if (isLiked) {
                playerLikeBtn.innerHTML = '<i class="fas fa-heart text-green-500"></i>';
            } else {
                playerLikeBtn.innerHTML = '<i class="far fa-heart"></i>';
            }
        }
        
        let trackToAdd = null;
        async function openPlaylistModal(videoId) {
            trackToAdd = videoId;
            const response = await fetch('/api/library_data');
            const data = await response.json();
            const listEl = document.getElementById('modal-playlist-list');
            listEl.innerHTML = '';
            data.playlists.forEach(p => {
                const li = document.createElement('li');
                li.className = 'p-2 hover:bg-white/10 rounded cursor-pointer';
                li.textContent = p.name;
                li.onclick = () => addTrackToPlaylist(p.id);
                listEl.appendChild(li);
            });
            document.getElementById('modal-backdrop').classList.remove('hidden');
        }
        
        function closePlaylistModal() {
            document.getElementById('modal-backdrop').classList.add('hidden');
        }
        
        async function addTrackToPlaylist(playlistId) {
            if (!trackToAdd) return;
             await fetch('/api/playlists/add_song', {
                 method: 'POST',
                 headers: {'Content-Type': 'application/json'},
                 body: JSON.stringify({playlistId, videoId: trackToAdd})
             });
             closePlaylistModal();
             alert('Şarkı eklendi!');
        }

        // --- Başlangıç ---
        document.addEventListener('DOMContentLoaded', () => {
            loadPage('index');
            updateSidebarPlaylists();
            audioPlayer.volume = volumeBar.value / 100;
        });
    </script>
</body>
</html>
"""

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="tr" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Giriş Yap - Müzik Platformu</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap" rel="stylesheet">
    <style> body { font-family: 'Inter', sans-serif; } </style>
</head>
<body class="bg-black text-white flex items-center justify-center min-h-screen">
    <div class="w-full max-w-md p-8 space-y-8 bg-[#121212] rounded-lg shadow-lg">
        <div>
            <h1 class="text-3xl font-bold text-center">Müzik Platformu</h1>
            {% if error %}
            <p class="mt-4 text-center text-red-400 bg-red-900/50 p-3 rounded-md">{{ error }}</p>
            {% endif %}
        </div>
        
        <div x-data="{ tab: 'login' }" class="space-y-4">
            <div class="flex border-b border-gray-700">
                <button @click="tab = 'login'" :class="{'border-white text-white': tab === 'login', 'border-transparent text-gray-400': tab !== 'login'}" class="flex-1 py-2 text-center font-bold border-b-2 transition">Giriş Yap</button>
                <button @click="tab = 'register'" :class="{'border-white text-white': tab === 'register', 'border-transparent text-gray-400': tab !== 'register'}" class="flex-1 py-2 text-center font-bold border-b-2 transition">Kayıt Ol</button>
            </div>
            
            <!-- Giriş Formu -->
            <form x-show="tab === 'login'" action="{{ url_for('login') }}" method="post" class="space-y-6">
                <input type="hidden" name="action" value="login">
                <div>
                    <label for="login-username" class="sr-only">Kullanıcı Adı</label>
                    <input id="login-username" name="username" type="text" required class="w-full px-4 py-3 bg-[#2a2a2a] border border-gray-600 rounded-md focus:outline-none focus:ring-2 focus:ring-green-500" placeholder="Kullanıcı Adı">
                </div>
                <div>
                    <label for="login-password" class="sr-only">Şifre</label>
                    <input id="login-password" name="password" type="password" required class="w-full px-4 py-3 bg-[#2a2a2a] border border-gray-600 rounded-md focus:outline-none focus:ring-2 focus:ring-green-500" placeholder="Şifre">
                </div>
                <button type="submit" class="w-full py-3 font-bold text-black bg-green-500 rounded-full hover:bg-green-600 transition">Giriş Yap</button>
            </form>
            
            <!-- Kayıt Formu -->
            <form x-show="tab === 'register'" action="{{ url_for('login') }}" method="post" class="space-y-6">
                <input type="hidden" name="action" value="register">
                <div>
                    <label for="register-username" class="sr-only">Kullanıcı Adı</label>
                    <input id="register-username" name="username" type="text" required class="w-full px-4 py-3 bg-[#2a2a2a] border border-gray-600 rounded-md focus:outline-none focus:ring-2 focus:ring-green-500" placeholder="Kullanıcı Adı">
                </div>
                <div>
                    <label for="register-password" class="sr-only">Şifre</label>
                    <input id="register-password" name="password" type="password" required class="w-full px-4 py-3 bg-[#2a2a2a] border border-gray-600 rounded-md focus:outline-none focus:ring-2 focus:ring-green-500" placeholder="Şifre">
                </div>
                 <button type="submit" class="w-full py-3 font-bold text-black bg-green-500 rounded-full hover:bg-green-600 transition">Kayıt Ol</button>
            </form>
        </div>
    </div>
    <script src="//unpkg.com/alpinejs" defer></script>
</body>
</html>
"""


@app.route('/', methods=['GET', 'POST'])
def login():
    if 'username' in session:
        return redirect(url_for('index'))

    error = None
    if request.method == 'POST':
        action = request.form['action']
        username = request.form['username']
        password = request.form['password']
        
        passwords_data = read_json(PASSWORDS_FILE, {})

        if action == 'register':
            if username in passwords_data:
                error = "Bu kullanıcı adı zaten alınmış."
            elif len(password) < 4:
                 error = "Şifre en az 4 karakter olmalı."
            else:
                passwords_data[username] = generate_password_hash(password)
                write_json(PASSWORDS_FILE, passwords_data)
                session['username'] = username
                session.permanent = True
                return redirect(url_for('index'))

        elif action == 'login':
            user_hash = passwords_data.get(username)
            if user_hash and check_password_hash(user_hash, password):
                session['username'] = username
                session.permanent = True
                return redirect(url_for('index'))
            else:
                error = "Geçersiz kullanıcı adı veya şifre."
    
    return render_template_string(LOGIN_TEMPLATE, error=error)

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('login'))

@app.route('/index')
@login_required
def index():
    return render_template_string(MAIN_TEMPLATE)


if __name__ == '__main__':
    # Popüler şarkıları başlangıçta bir kez güncelle
    threading.Thread(target=update_popular_tracks).start()
    # Arka plan güncelleyiciyi başlat
    update_thread = threading.Thread(target=background_track_updater, daemon=True)
    update_thread.start()
    
    # Uygulamayı çalıştır
    app.run(debug=True, host='0.0.0.0', port=5000)
