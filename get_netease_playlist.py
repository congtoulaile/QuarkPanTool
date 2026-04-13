import requests


def get_playlist_full(playlist_id):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    # 1. 先拿歌单名 + 全部歌曲ID
    url_detail = f"https://music.163.com/api/v1/playlist/detail?id={playlist_id}"
    res = requests.get(url_detail, headers=headers)
    data = res.json()

    playlist_name = data["playlist"]["name"]
    track_ids = [str(t["id"]) for t in data["playlist"]["trackIds"]]

    # 2. 分批获取所有歌曲详情（每批最多200首，避免请求过大）
    song_list = []
    batch_size = 200
    for start in range(0, len(track_ids), batch_size):
        batch_ids = track_ids[start:start + batch_size]
        # 构造 POST 请求体，用 c 参数传递歌曲ID列表
        c_param = [{"id": tid} for tid in batch_ids]
        res_songs = requests.post(
            "https://music.163.com/api/v3/song/detail",
            headers=headers,
            data={"c": str(c_param).replace("'", '"')},
        )
        songs_data = res_songs.json()

        for s in songs_data["songs"]:
            song_name = s["name"]
            artist = "/".join([a["name"] for a in s["ar"]])
            song_list.append(f"{song_name} - {artist}")

    return playlist_name, song_list


if __name__ == "__main__":
    # 替换成你的歌单ID
    playlist_id = "你的歌单ID"
    name, songs = get_playlist_full(playlist_id)

    print("歌单名称：", name)
    print("=" * 50)
    for i, song in enumerate(songs, 1):
        print(f"{i:2d}. {song}")
