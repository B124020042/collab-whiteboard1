#!/usr/bin/env python3
"""把白板打包成執行檔 (不需要安裝 Python 就能執行)。

    pip install pyinstaller certifi
    python build.py

產生的檔案放在 release/：
  Windows : CollabWhiteboard-Windows.exe   (單一執行檔)
  macOS   : CollabWhiteboard-macOS.zip     (解壓縮後是 CollabWhiteboard.app)

config.json (伺服器網址) 與 SSL 憑證 (certifi) 會一起包進去。
GitHub Actions (.github/workflows/build.yml) 每次 push 都會執行這個腳本並發布到 Releases。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "CollabWhiteboard"


def main():
    os.chdir(HERE)
    for d in ("build", "dist", "release"):
        shutil.rmtree(d, ignore_errors=True)
    mac = sys.platform == "darwin"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--name", NAME, "--add-data", f"config.json{os.pathsep}.",
           "--hidden-import", "certifi", "--hidden-import", "client"]
    if mac:
        cmd += ["--osx-bundle-identifier", "io.github.collab-whiteboard"]   # macOS：.app 資料夾
    else:
        cmd += ["--onefile"]                                                # Windows：單一 .exe
    subprocess.run(cmd + ["start.py"], check=True)

    out = HERE / "release"
    out.mkdir()
    if mac:
        target = out / f"{NAME}-macOS.zip"
        # ditto 會保留 .app 裡的符號連結與簽章，shutil 壓縮會弄壞
        subprocess.run(["ditto", "-c", "-k", "--keepParent", f"dist/{NAME}.app", str(target)],
                       check=True)
    else:
        target = out / f"{NAME}-Windows.exe"
        shutil.copy(HERE / "dist" / f"{NAME}.exe", target)
    print(f"完成：{target} ({target.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
