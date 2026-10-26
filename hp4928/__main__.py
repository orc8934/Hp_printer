"""允许 `python3 -m hp4928` 直接启动。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
