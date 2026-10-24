# 也许是时候反击你那吵闹的室友了 - 波形发生器

[English](README.md) | 简体中文 Simplified Chinese（本文档）

我受够了。
每一天，我的室友都在语音通话里玩《三角洲行动》或其他游戏。
他们就不能去网吧玩吗？！或者至少声音小一点啊？！
他们一直玩到深夜；那噪音让我根本无法睡觉，甚至没法集中注意力做任何事情。
行吧。那很好，你不仁你也别怪我不义。

> 免责声明：本程序并非旨在造成任何身体伤害；请勿将其用于任何非法或不当用途。话虽如此……如果你的室友真的逼得你不得不对他使用这种武器，那么那位室友恐怕也称不上是人类了。

## 特点

它就是一个具备所有标准功能的波形发生器——仅此而已，没什么特别的。

## 构建与安装

你需要：
- Python 3.12
- Windows 10/11

**强烈建议使用 [uv](https://github.com/astral-sh/uv) 进行高效的依赖管理。**

### 编译方法 1（强烈推荐）

如果你已安装 `uv`，请直接执行 `uv sync` 命令，并在安装完成后进行下一步。

### 编译方法 2（传统方法）

如果你没有安装 `uv`，请确保你的 Python 版本为 3.12 或更高版本，然后...
首先，执行命令 `python -m venv .venv` 来创建虚拟环境。
然后，执行命令 `.venv\Scripts\activate` (Windows) 或 `source .venv/bin/activate` (macOS / Linux) 来激活环境。
最后，执行命令 `pip install -r requirements.txt` 来安装依赖项。
如果安装成功完成，你可以继续下一步。如果在安装过程中发生错误，请删除虚拟环境并重试，或者使用 `uv` 以获得更顺畅的安装过程。

## 调试与运行

在文件根目录下执行 `uv run main.py`。
或者执行命令 `.venv\Scripts\activate` (Windows) 或 `source .venv/bin/activate` (macOS / Linux) 来激活环境，然后执行 `python main.py`。

## 生产打包

执行 `uv run pyinstaller -F --noconsole --add-data "assets;assets" --name "MiTtRAYNR-WfG" app.py`。
或者执行命令 `.venv\Scripts\activate` (Windows) 或 `source .venv/bin/activate` (macOS / Linux) 来激活环境，然后执行 `pyinstaller -F --noconsole --add-data "assets;assets" --name "MiTtRAYNR-WfG" app.py`。

## 备注

- 由于使用了 PyInstaller，本程序生成的 EXE 文件可能会被各种杀毒软件立即检测并隔离。
- 警告：首次运行时，请务必将音量调至最低，以防止突发的高音量损伤你的听力！即使你想报复你的室友，也请考虑一下你自己的听力！
- 所用的 Demo 音频：《今宵是飘逸的利己主义者　～ Egoistic Flowers.》，毕竟你的舍友可能真的是个利己主义者，否则你不可能用这个东西攻击他

## 许可证

MIT 许可证 - 欢迎随意分叉（fork）并进行尝试，但请不要将其用于任何真正的恶意目的。对于因使用本项目而导致的任何意外（例如受害者因无法忍受噪音而摔碎键盘、电脑或在现实生活中发生冲突），作者不承担任何责任。（然而，当室友制造了如此难以忍受的噪音时，他们自己肯定也能忍受同等程度的噪音吧？受不了？那么也许他们应该反思一下自己的行为。）
