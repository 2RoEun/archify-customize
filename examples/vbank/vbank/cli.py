import sys

from .reports import summary


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    print(summary(argv[0], argv[1:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
