from __future__ import annotations

from .canonical_env import load_project_env


def main() -> None:
    load_project_env()
    from .canonical_shadow import main as shadow_main
    shadow_main()


if __name__ == "__main__":
    main()
