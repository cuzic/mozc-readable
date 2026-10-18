# mozc-readable

**English summary**: an unofficial build of [Mozc](https://github.com/google/mozc) (the open-source edition of Google Japanese Input) whose TSF text service writes the IME's on/off state and input mode to a named shared-memory block, so other processes can read it without any IPC handshake. It is meant for tools that need to know the real IME state, such as keyboard remappers ([awase](https://github.com/cuzic/awase)) and the host-side bridge of a custom keyboard (QMK etc.) that shows the state on an OLED or switches layers. Windows only. Not affiliated with Google. Specification: [docs/shared-memory.md](docs/shared-memory.md) (in Japanese). Status: experimental, no release yet, unsigned.

---

Mozc に、**IME の開閉と入力モードを共有メモリへ書き出す**変更を加えた非公式のビルドです。他のプロセスは、IPC の手続きなしで、いまの IME の状態を読めます。

## 誰のためのものか

- **自作キーボード(QMK など)を作る人**: IME の状態を OLED・LED・レイヤーに連動させたい。PC 側の小さなブリッジが共有メモリを読み、変わったときに USB(HID)でキーボードへ送ります。
- **キーボードのリマッパーなど、IME の本当の状態が要るツール**: たとえば [awase](https://github.com/cuzic/awase)(親指シフトのリマッパー)。Windows の標準の API では、他のアプリの IME の状態を外から確実には読めません。

## 状況

- **実験段階**です。Release はまだありません。インストーラー(MSI)に**署名はなく**、SmartScreen の警告が出ます。
- Windows 専用で、**この IME(Mozc Readable)を入力方式にした窓だけ**で値が出ます。公式の Mozc・Google 日本語入力・Microsoft IME には書き出し側がありません。読めない窓は「不明」として扱ってください。
- 共有メモリの仕様は **version 2** です(`readable` ブランチの `aa228f6`)。`active` で「いま mozc-readable が使われているか」を区別します。**CI の確認中で、リリース済みのビルドはまだありません**(IME を切り替えたあとの `active=0` の検査は未検証)。詳細は仕様書の「既知の限界」。
- 公式の Mozc とは別名(別の識別子・別のインストール先)にしてありますが、**同時にインストールして共存できるかの検査はまだありません**。

## 試す

1. MSI を入手してインストールし、入力方式に「Mozc Readable」を追加する(MSI は、Release が出るまでは GitHub Actions の成果物 `Mozc64_x64.msi` から取れます)。
2. 入力方式を Mozc Readable にした窓で、[`docs/examples/read_ime_state.py`](docs/examples/read_ime_state.py) を動かす(Windows、標準ライブラリだけ)。

   ```text
   uv run readable/docs/examples/read_ime_state.py
   ```

   IME の開閉・モードを切り替えると、表示が変わります。**この例は Windows 実機で動かしていません**(構文の確認のみ)。動かして食い違えば、仕様書と実装が正です。
3. キーボードへ送るところは、仕様書の「想定読者と使い道: 自作キーボード」を参照してください(QMK の Raw HID のスケッチを載せています。こちらも未検証です)。

## 仕様書

[docs/shared-memory.md](docs/shared-memory.md): オブジェクト名、48 バイトの構造体、入力モードの値、読み手の規約(seqlock の手順、受け入れ条件)、AppContainer(UWP など)の窓の読み方、既知の限界、version 2 の予定。

## このリポジトリの構成

- upstream(google/mozc)からの差分は、`readable` ブランチにあります。書き出しの本体は `src/win32/tip/tip_state_publisher.{h,cc}` で、`tip_input_mode_manager` から呼びます。そのほか、公式 Mozc と別名にするための変更(識別子・名前)があります。
- `readable/` は、この fork 独自のもの(CI、仕様書など)で、upstream には無いディレクトリです。
- upstream の新しいリリースへの自動追随と、Release の自動公開は、整備中です([PR #1](https://github.com/cuzic/mozc-readable/pull/1) と、awase の [ADR-265](https://github.com/cuzic/awase/blob/develop/docs/adr/265-mozc-readable-integration.md))。

## ライセンス

Mozc と同じ BSD-3-Clause です(Google の名称・商標を宣伝には使えません)。Google とは無関係の非公式の派生版です。
