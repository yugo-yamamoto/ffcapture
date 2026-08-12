# ffcapture

Windows 用の画面キャプチャ **ffmpeg コマンドラインビルダー**（Tkinter GUI）。

GUI は録画をしません。素の `ffmpeg` コマンドを組み立てるだけなので、コピーして
端末・バッチ・タスクスケジューラのどこに貼ってもそのまま動きます。

![ffcapture の画面](docs/screenshot.png)

## 必要なもの

- Windows 10 / 11
- Python 3.10 以降（`python.exe` / `pythonw.exe`）— **依存パッケージなし。標準ライブラリのみ**
- [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) — **PATH を通しておくこと**。生成されるコマンドは
  フルパスではなく素の `ffmpeg` で始まるので、そのまま他の PC にも貼れます

## 使い方

**`ffcapture.pyw` をダブルクリック**すれば GUI が開きます。拡張子が `.pyw` なので
`pythonw.exe` で起動し、黒いコンソール窓は出ません。

GUI は**コマンドを組み立てるだけ**です。録画はしません。項目を変えるたびに
「生成されたコマンド」が作り直されるので、「コマンドをコピー」で貼り付けて実行してください。

```console
pythonw ffcapture.pyw            ダブルクリックと同じ（コンソール窓なし）
python  ffcapture.pyw            コンソール付きで起動（ログを端末でも見たいとき）
python  ffcapture.pyw --list     音声エンドポイントと DirectShow デバイスを一覧表示
```

### ダブルクリックが効かないとき

`.pyw` に関連付けが無いと、Windows は「このファイルを開く方法を選んでください」を出します。
python.org 版の Python は既定で関連付けますが、**Microsoft Store 版では `.pyw` が
未設定のまま**のことがあります。その場合は一度だけ実行してください:

```console
python ffcapture.pyw --register-pyw     .pyw を pythonw.exe に関連付ける
python ffcapture.pyw --unregister-pyw   取り消す
```

`HKCU\Software\Classes` に書くだけなので管理者権限は不要です。ただし
**この PC の `.pyw` ファイル全体**が対象になる点だけ注意してください
（現在の割り当ては `--list` の末尾で確認できます）。

## できること

### 映像

| モード | 生成されるオプション |
| --- | --- |
| 画面全体 | `-f lavfi -i ddagrab=output_idx=0:framerate=30:draw_mouse=1` |
| 矩形指定 | 上に `:video_size=WxH:offset_x=X:offset_y=Y` を追加 |
| ウィンドウ | `-f gdigrab -i title=<ウィンドウタイトル>` |

gdigrab を選んだ場合は `-f gdigrab -offset_x X -offset_y Y -video_size WxH -i desktop` になります。
ddagrab は D3D11 テクスチャを返すので、CPU エンコーダに渡す前に `hwdownload,format=bgra` を挟みます。

- ウィンドウ一覧は EnumWindows で列挙（非表示の UWP ウィンドウは除外）
- 画面をドラッグして範囲を選ぶオーバーレイ付き。マルチモニタの負座標にも対応
- プロセスを DPI aware にしてあるので、拡大表示（125% / 150%）でも座標がずれない

> **ウィンドウ指定の注意**
> gdigrab のウィンドウ指定はウィンドウ DC を BitBlt する方式のため、ハードウェア描画の
> アプリ（ブラウザ・Electron・ゲーム等）では黒画面や文字欠けになります。
> その場合は「選択ウィンドウ → 矩形指定に変換」ボタンで矩形指定に切り替えてください。

### 取り込み方式 — カクつくときはここ

| 方式 | 実測キャプチャレート（全画面 2880x1800 / 30fps 指定） |
| --- | --- |
| **ddagrab**（既定） | **29.1 fps** |
| gdigrab | 17.8 fps |

`gdigrab` はウィンドウ DC を BitBlt でコピーするため、高解像度の全画面では 30fps
指定でも実際には 18fps 前後しか取り込めず、足りない分は同じ画を複製して埋めます
（`dup=122`）。これが「カクカク」の正体です。

**ddagrab**（Desktop Duplication API）は GPU 側で画面を取得するので大幅に速く、
30fps 指定でほぼ 30fps、60fps 指定でも 58fps 出ます。矩形指定・全画面で使えるので既定にしてあります。

- **ウィンドウタイトル指定は gdigrab にしかありません。** ウィンドウを選ぶと自動で
  gdigrab に切り替わり、他のモードに戻すと ddagrab に復帰します
- ddagrab を持たない ffmpeg では自動的に gdigrab のみになります
- ddagrab の矩形は**選択したモニタの左上が原点**です（gdigrab は仮想デスクトップ基準）

エンコーダが追いつかない場合（ログの `speed=` が 1.0x を下回る場合）は、出力サイズを
1080p に落とすか、ハードウェアエンコーダを選んでください。

### 音声（DirectShow）

`-f dshow -i audio="<デバイス名>"` を組み立てます。デバイス一覧は
`ffmpeg -list_devices true -f dshow -i dummy` から取得します。

**PC で再生中の音**を録るにはステレオミキサー等のループバック用デバイスが必要です。
Realtek などのドライバが持っていても既定で無効化されていることが多いため、
このツールはレジストリ
（`HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture`）の
`DeviceState` を読んで状態を判定します。

| DeviceState | 意味 |
| --- | --- |
| `0x00000001` | 有効 |
| `0x10000001` | 無効（ユーザーが無効化） |
| `0x0000000?4` | 未接続（過去のハードウェアの残骸） |
| `0x00000008` | プラグ未挿入 |

無効なステレオミキサーが見つかった場合は「ステレオミキサーを有効化（管理者）」ボタンが
有効になります。押すと UAC 付きの PowerShell で `DeviceState=1` に書き換え、
`AudioEndpointBuilder` を再起動してから再列挙します（数秒だけ音が途切れます）。

#### 録れているか確かめる

「音量を3秒テスト」を押すと、選択中のデバイスから 3 秒録って
`volumedetect` で平均・ピークを dB で表示します。音が入っていないときの切り分け用です。

| ピーク | 意味 |
| --- | --- |
| −60 dB 以下 | 実質無音。何も再生していないか、**別の再生デバイスに音が流れている** |
| −60 〜 −30 dB | 拾えてはいるが小さい。録音レベルか再生音量を上げる |
| −30 dB 以上 | 十分 |

**ステレオミキサーが拾えるのは、それが属する再生デバイスに流れた音だけです。**
USB や Bluetooth のヘッドセット・スピーカーで再生していると、
Realtek のステレオミキサーは無音を録り続けます。この場合は再生先を
そのサウンドカードに戻すか、VB-CABLE のような仮想デバイスを使ってください。

ステレオミキサー自体が存在しない構成では、
[VB-CABLE](https://vb-audio.com/Cable/) や
[screen-capture-recorder](https://github.com/rdp/screen-capture-recorder-to-video-windows-free)
（`virtual-audio-capturer`）を導入すると同じ方法で録音できます。

### エンコード

`libx264` / `libx265` / `h264_nvenc` / `hevc_nvenc` / `h264_qsv` / `h264_amf` に対応。
起動時に**各エンコーダで実際に 1 フレームだけエンコードしてみて**、通ったものだけを候補に出します。
`ffmpeg -encoders` はビルドに含まれるかを示すだけで GPU の有無は分からず、NVIDIA が無い機体でも
`h264_nvenc` が一覧に載ってしまうためです。
品質指定（CRF / CQ / QP / global_quality）はエンコーダごとに適切なオプションへ振り分けます。

### 出力先

**カレントディレクトリに出力します。** 指定するのはファイル名だけで、保存先ダイアログはありません。
生成されるコマンドの末尾も `capture_20260807_194530.mp4` のような相対パスになるので、
コピーして別のフォルダやバッチに貼れば、そのフォルダに出ます。

GUI はコマンドを組み立てるだけで録画は行いません。出力先は**コマンドを貼り付けて実行した
シェルのカレントディレクトリ**になります。

### 最大録画時間

「最大録画時間 (-t)」をチェックすると `-t <時間>` を出力側に付けます。
指定した時間が経つと ffmpeg が自分から**正常終了**するので、MP4 でもファイルが壊れません。

- 書式は `HH:MM:SS`（例 `00:05:00`）か秒数（例 `300`、`90.5`）
- 入力欄の右に解釈結果が秒で表示され、書式が不正なら赤字で警告します

### 音声オフセット（既定 1000 ms）

**無補正だと音声が映像より約 1 秒先行します。** DirectShow の音声は録画開始の時点で
すでに 1 秒ぶんほど溜まった状態から流れ始めるらしく、その古い音が出力の 0 秒に貼り付きます。
`-itsoffset` で音声を遅らせて相殺します。

基準クリップを再生しながら録画し、フラッシュとビープの時刻を突き合わせた実測:

| 条件 | ずれ |
| --- | --- |
| ddagrab / `aresample=async=1:first_pts=0`（従来） | −1033 ms |
| ddagrab / `first_pts=0` を外す | −1000 ms |
| ddagrab / `-af` 自体を外す | −967 ms |
| **gdigrab**（映像側を変えても同じ） | −1033 ms |
| **`-itsoffset 1.0` を追加** | **+0 / +33 / −67 ms** |

映像バックエンドを変えても `-af` を外しても変わらないので、**映像側ではなく dshow 音声側**の問題です。
ずれは全区間で一定（ドリフトなし）でした。

音声デバイスに依存する値なので、環境が変わったら次の節の手順で測り直してください。

## ずれを実測する

同期がおかしいと感じたら、目視ではなく数値で確かめられます。

```console
python tools/make_synctest.py            synctest.mp4 を作る（1280x720 / 60fps / 30秒）
（synctest.mp4 を全画面で再生しながら ffcapture で録画する）
python tools/analyze_capture.py capture.mp4
```

`synctest.mp4` は **画面全体の白フラッシュと 1kHz のビープが同時に出る**クリップです
（30 秒間に 22 回、不等間隔）。両者は同じ式から生成しているのでサンプル単位で一致しています。
画面下部を走るシアンのバーはコマ落ちの目視用です。

`analyze_capture.py` は録画結果の輝度波形と音声振幅波形を ±3 秒ずらしながら相関を取り、
いちばん一致する位置をずれ量として出します。1 回のイベントではなく 22 回ぶんを使うので、
単発のカチンコより安定します。

```
実効更新レート: 25.7 fps（30 fps に対して 86%）
A/V ずれ      : -80 ms  (相関 0.31)
  → 音声が 80 ms 先行しています。「音声オフセット (ms)」を今より 80 ms 大きくしてください。
```

出た値を「音声オフセット (ms)」に足し引きして、ずれが ±25 ms 以内に入れば十分です。

`synctest.mp4` のフラッシュは**わざと不等間隔**にしてあります。等間隔だと相関のピークが
その周期ごとに繰り返し、たとえば −867 ms と +133 ms を区別できません（実際にこれで
測定を誤りました）。不等間隔なら ±3 秒の範囲でも一意に決まります。

> 解析自体にも AAC のプライミング等で数十 ms のバイアスが乗るため、
> 同じディレクトリの `synctest.mp4` を基準に自動で較正します。
> 既知のずれ（−100 / −400 / +900 ms）を与えたファイルで、較正後は誤差 0 ms で
> 復元することを確認済みです。

**測るときの注意**: 録画の負荷で再生アプリの映像がコマ落ちすると、その遅れがそのまま
「音声が先行」として出ます。まず出力サイズやエンコーダを調整して `speed=` が 1.0x を
下回らない状態にしてから測ってください。

## 生成されるコマンドの例

```
ffmpeg -hide_banner -y -f lavfi -thread_queue_size 1024 ^
  -i ddagrab=output_idx=0:framerate=30:draw_mouse=1:video_size=1280x720:offset_x=100:offset_y=200 ^
  -f dshow -rtbufsize 256M -thread_queue_size 1024 -audio_buffer_size 50 ^
  -itsoffset 1.000 -i "audio=ステレオ ミキサー (Realtek(R) Audio)" ^
  -map 0:v:0 -map 1:a:0 -c:v libx264 -preset veryfast -crf 23 ^
  -vf "hwdownload,format=bgra,scale=trunc(iw/2)*2:trunc(ih/2)*2" -pix_fmt yuv420p ^
  -c:a aac -b:a 192k -af aresample=async=1:first_pts=0 ^
  -movflags +faststart capture_20260810_144059.mp4
```

## 停止のしかた

貼り付けて実行した ffmpeg は、`q` を押すと trailer を書いて正常終了します
（`Ctrl+C` でも可）。MP4 は正常終了しないと壊れるため、中断が心配な場合は
`.mkv` で出力するか、「最大録画時間 (-t)」を設定して ffmpeg に自分で終わらせてください。
