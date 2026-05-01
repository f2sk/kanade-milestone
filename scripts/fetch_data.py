"""
YouTube Data API v3 から再生数履歴を取得・更新するスクリプト

実行方法:
    YOUTUBE_API_KEY=AIza... python scripts/fetch_data.py

環境変数:
    YOUTUBE_API_KEY  YouTube Data API v3 のキー（必須）

依存ライブラリ:
    requests（Python 3.8+）

動作:
    1. public_config.json からチャンネル設定・マーク・追加URLを読み込む
    2. YouTube API でチャンネル動画と追加動画の最新再生数を取得
    3. public_data.json に履歴を追記（7日分保持）して上書き保存
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

API_KEY    = os.environ.get('YOUTUBE_API_KEY', '')
BASE_URL   = 'https://www.googleapis.com/youtube/v3'
DATA_FILE  = 'public_data.json'
CONFIG_FILE = 'public_config.json'
HISTORY_DAYS = 7


def yt_get(endpoint, **params):
    params['key'] = API_KEY
    r = requests.get(f'{BASE_URL}/{endpoint}', params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def get_channel_id(handle):
    data = yt_get('channels', part='id', forHandle=handle)
    items = data.get('items', [])
    if not items:
        raise ValueError(f'チャンネルが見つかりません: {handle}')
    return items[0]['id']


def get_uploads_playlist(channel_id):
    data = yt_get('channels', part='contentDetails', id=channel_id)
    return data['items'][0]['contentDetails']['relatedPlaylists']['uploads']


def get_video_ids_from_playlist(playlist_id):
    ids = []
    page_token = None
    while True:
        params = dict(part='contentDetails', playlistId=playlist_id, maxResults=50)
        if page_token:
            params['pageToken'] = page_token
        data = yt_get('playlistItems', **params)
        for item in data.get('items', []):
            ids.append(item['contentDetails']['videoId'])
        page_token = data.get('nextPageToken')
        if not page_token:
            break
    return ids


def get_video_details(video_ids):
    result = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        data = yt_get('videos', part='snippet,contentDetails,statistics', id=','.join(batch))
        for item in data.get('items', []):
            vid     = item['id']
            snippet = item['snippet']
            stats   = item.get('statistics', {})
            result[vid] = {
                'id':          vid,
                'title':       snippet['title'],
                'channelId':   snippet['channelId'],
                'thumbnail':   snippet['thumbnails'].get('medium', {}).get('url', ''),
                'duration':    parse_duration(item['contentDetails']['duration']),
                'publishedAt': snippet['publishedAt'],
                'views':       int(stats.get('viewCount', 0)),
            }
    return result


def parse_duration(iso):
    """ISO 8601 duration (PT1H2M3S) → "1:02:03" 形式"""
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso)
    h, mn, s = (int(x or 0) for x in m.groups())
    if h:
        return f'{h}:{mn:02d}:{s:02d}'
    return f'{mn}:{s:02d}'


def main():
    if not API_KEY:
        print('ERROR: YOUTUBE_API_KEY が設定されていません', file=sys.stderr)
        sys.exit(1)

    # 設定読み込み
    with open(CONFIG_FILE, encoding='utf-8') as f:
        config = json.load(f)

    channel_handle  = config.get('channelHandle', '')
    own_channel_id  = config.get('ownChannelId', '')
    pinned_ids      = config.get('pinnedVideoIds', [])

    # 既存データ読み込み
    existing_videos = {}
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding='utf-8') as f:
            existing = json.load(f)
        existing_videos = existing.get('videos', {})
        if not own_channel_id:
            own_channel_id = existing.get('ownChannelId', '')

    # チャンネルID解決
    if not own_channel_id and channel_handle:
        print(f'チャンネルID を解決中: {channel_handle}')
        own_channel_id = get_channel_id(channel_handle)
        print(f'  → {own_channel_id}')

    # チャンネル動画一覧取得
    channel_video_ids = []
    if own_channel_id:
        uploads_pl = get_uploads_playlist(own_channel_id)
        channel_video_ids = get_video_ids_from_playlist(uploads_pl)
        print(f'チャンネル動画: {len(channel_video_ids)} 件')

    # 全取得対象（重複排除）
    all_ids = list(dict.fromkeys(channel_video_ids + pinned_ids))
    print(f'合計取得対象: {len(all_ids)} 件')

    # 動画詳細・再生数取得
    details = get_video_details(all_ids)
    print(f'取得成功: {len(details)} 件')

    # 履歴更新
    now_ms   = int(time.time() * 1000)
    cutoff   = now_ms - HISTORY_DAYS * 86400 * 1000

    for vid, info in details.items():
        views  = info.pop('views')
        prev   = existing_videos.get(vid, {})
        history = [h for h in prev.get('history', []) if h['ts'] >= cutoff]
        # 直近と同じ再生数なら追記しない（不要な差分コミットを防ぐ）
        if not history or history[-1]['views'] != views:
            history.append({'ts': now_ms, 'views': views})
        existing_videos[vid] = {**info, 'history': history}

    # チャンネルから消えた動画はpinnedでなければ除外
    keep_ids = set(channel_video_ids) | set(pinned_ids)
    existing_videos = {k: v for k, v in existing_videos.items() if k in keep_ids}

    output = {
        'lastUpdated':  now_ms,
        'ownChannelId': own_channel_id,
        'videos':       existing_videos,
    }

    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, separators=(',', ':'))

    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f'完了: {len(existing_videos)} 件を {ts} に更新')


if __name__ == '__main__':
    main()
