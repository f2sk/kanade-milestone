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
    3. 併せてチャンネル登録者数と配信情報（配信種別・開始終了時刻・実配信時間）を取得
       いずれも既存の呼び出しにpartを相乗りさせるだけなのでクォータ増加なし
    4. public_data.json に履歴を追記（直近7日は30分刻み・7日超は2時間刻みで最大30日保持）して上書き保存
       登録者数は channel.subscriberHistory に変化点のみ記録（同じく最大30日保持）
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
HISTORY_DAYS = 30                    # 履歴の最大保持日数
FINE_DAYS = 7                        # 直近この日数は30分刻みのまま保持（外挿予測用）
COARSE_BUCKET_MS = 2 * 3600 * 1000   # 7日より古い部分は2時間バケットに間引く（同バケットは最新のみ）


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


def get_channel_info(channel_id):
    """uploadsプレイリストIDと登録者数を1回のchannels.listで取得。

    channels.list はpartを増やしてもクォータ1unitのままなので、
    statistics を相乗りさせても追加コストはゼロ。
    登録者数を非公開にしているチャンネルでは subscriberCount が欠けるため None を返す。
    """
    data = yt_get('channels', part='contentDetails,statistics', id=channel_id)
    item = data['items'][0]
    stats = item.get('statistics', {})
    subs = stats.get('subscriberCount')
    return {
        'uploads': item['contentDetails']['relatedPlaylists']['uploads'],
        'subscribers': int(subs) if subs is not None else None,
    }


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
    """videos.list は part を増やしてもクォータ1unit/呼び出しのままなので
    liveStreamingDetails を相乗りさせても追加コストはゼロ。"""
    result = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        data = yt_get('videos', part='snippet,contentDetails,statistics,liveStreamingDetails',
                      id=','.join(batch))
        for item in data.get('items', []):
            vid     = item['id']
            snippet = item['snippet']
            stats   = item.get('statistics', {})
            info = {
                'id':          vid,
                'title':       snippet['title'],
                'channelId':   snippet['channelId'],
                'thumbnail':   snippet['thumbnails'].get('medium', {}).get('url', ''),
                'duration':    parse_duration(item.get('contentDetails', {}).get('duration')),
                'publishedAt': snippet['publishedAt'],
                'views':       int(stats.get('viewCount', 0)),
            }
            live = build_live_info(snippet, item.get('liveStreamingDetails'))
            if live:
                info['live'] = live
            result[vid] = info
    return result


def build_live_info(snippet, lsd):
    """配信・プレミア枠の情報をまとめる。通常のアップロード動画では None を返す。

    liveBroadcastContent は none / upcoming / live のいずれか。
    liveStreamingDetails はアーカイブにも残り actualStartTime/actualEndTime が取れるので、
    実配信時間はリアルタイム性を要求されない（終了後に1回拾えば確定する）。

    同接(concurrentViewers)は意図的に取得しない。配信の中央値が約1.9時間で
    30分間隔では1配信あたり3〜4点しか拾えず、真のピークをまず捉えられないため。
    下限値を保存すると後でピーク値として誤読される危険の方が大きい。
    必要になったら1分間隔で叩ける常時稼働機側で別系統として取ること。
    """
    state = snippet.get('liveBroadcastContent', 'none')
    if state == 'none' and not lsd:
        return None
    lsd = lsd or {}
    live = {'state': state}
    for key, src in (('scheduled', 'scheduledStartTime'),
                     ('start', 'actualStartTime'),
                     ('end', 'actualEndTime')):
        if lsd.get(src):
            live[key] = lsd[src]
    # 実配信時間（秒）。開始・終了が揃ったアーカイブでのみ確定する
    if live.get('start') and live.get('end'):
        started = datetime.fromisoformat(live['start'].replace('Z', '+00:00'))
        ended   = datetime.fromisoformat(live['end'].replace('Z', '+00:00'))
        live['seconds'] = int((ended - started).total_seconds())
    return live


def parse_duration(iso):
    """ISO 8601 duration (PT1H2M3S) → 秒数（int）。配信予定枠等でdurationが無い場合は0"""
    if not iso:
        return 0
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso)
    if not m:
        return 0
    h, mn, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mn * 60 + s


def compact_history(history, fine_cutoff):
    """直近(fine_cutoff以降)は30分刻みのまま、それより古い部分は2時間バケットで最新のみ残す"""
    old_buckets = {}
    recent = []
    for h in history:  # history は時系列昇順
        if h['ts'] >= fine_cutoff:
            recent.append(h)
        else:
            old_buckets[h['ts'] // COARSE_BUCKET_MS] = h  # 同バケットは後勝ち＝最新
    old = [old_buckets[b] for b in sorted(old_buckets)]
    return old + recent


def main():
    if not API_KEY:
        print('ERROR: YOUTUBE_API_KEY が設定されていません', file=sys.stderr)
        sys.exit(1)

    # 設定読み込み
    with open(CONFIG_FILE, encoding='utf-8') as f:
        config = json.load(f)

    channel_handle      = config.get('channelHandle', '')
    own_channel_id      = config.get('ownChannelId', '')
    pinned_ids          = list(config.get('pinnedVideoIds', []))
    pinned_playlist_ids = config.get('pinnedPlaylistIds', [])

    # 既存データ読み込み
    existing_videos = {}
    sub_history = []
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding='utf-8') as f:
            existing = json.load(f)
        existing_videos = existing.get('videos', {})
        sub_history = existing.get('channel', {}).get('subscriberHistory', [])
        if not own_channel_id:
            own_channel_id = existing.get('ownChannelId', '')

    # チャンネルID解決
    if not own_channel_id and channel_handle:
        print(f'チャンネルID を解決中: {channel_handle}')
        own_channel_id = get_channel_id(channel_handle)
        print(f'  → {own_channel_id}')

    # 監視プレイリストから動画IDを展開して pinnedIds に追加
    for pl_id in pinned_playlist_ids:
        print(f'プレイリスト取得中: {pl_id}')
        pl_video_ids = get_video_ids_from_playlist(pl_id)
        print(f'  → {len(pl_video_ids)} 件')
        pinned_ids = list(dict.fromkeys(pinned_ids + pl_video_ids))

    # チャンネル動画一覧取得
    channel_video_ids = []
    subscribers = None
    if own_channel_id:
        ch_info = get_channel_info(own_channel_id)
        subscribers = ch_info['subscribers']
        channel_video_ids = get_video_ids_from_playlist(ch_info['uploads'])
        print(f'チャンネル動画: {len(channel_video_ids)} 件')
        print(f'登録者数: {subscribers if subscribers is not None else "非公開"}')

    # 全取得対象（重複排除）
    all_ids = list(dict.fromkeys(channel_video_ids + pinned_ids))
    print(f'合計取得対象: {len(all_ids)} 件')

    # 動画詳細・再生数取得
    details = get_video_details(all_ids)
    print(f'取得成功: {len(details)} 件')

    # 履歴更新（最大30日保持。直近7日は30分刻み、7日超は2時間刻みに間引く）
    now_ms      = int(time.time() * 1000)
    cutoff      = now_ms - HISTORY_DAYS * 86400 * 1000
    fine_cutoff = now_ms - FINE_DAYS * 86400 * 1000

    for vid, info in details.items():
        views  = info.pop('views')
        prev   = existing_videos.get(vid, {})
        history = [h for h in prev.get('history', []) if h['ts'] >= cutoff]
        # 追記（直近は30分刻みのまま。直近と同じ再生数なら追記しない）
        if not history or history[-1]['views'] != views:
            history.append({'ts': now_ms, 'views': views})
        # 7日より古い部分を2時間刻みへ間引く
        history = compact_history(history, fine_cutoff)
        existing_videos[vid] = {**info, 'history': history}

    # チャンネルから消えた動画はpinnedでなければ除外
    keep_ids = set(channel_video_ids) | set(pinned_ids)
    existing_videos = {k: v for k, v in existing_videos.items() if k in keep_ids}

    # 登録者数履歴（変化点のみ記録）
    # APIの subscriberCount は有効数字3桁に丸められるため値は階段状にしか動かない。
    # 勢いはステップの発生時刻から推定することになるので、30分毎に観測しつつ
    # 値が変わった時だけ点を打つ。同じ値が並ばないのでファイルはほとんど増えない。
    sub_history = [h for h in sub_history if h['ts'] >= cutoff]
    if subscribers is not None and (not sub_history or sub_history[-1]['subs'] != subscribers):
        sub_history.append({'ts': now_ms, 'subs': subscribers})

    output = {
        'lastUpdated':  now_ms,
        'ownChannelId': own_channel_id,
        'channel':      {'id': own_channel_id, 'subscriberHistory': sub_history},
        'videos':       existing_videos,
    }

    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, separators=(',', ':'))

    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f'完了: {len(existing_videos)} 件を {ts} に更新'
          f'（登録者履歴 {len(sub_history)} 点）')


if __name__ == '__main__':
    main()
