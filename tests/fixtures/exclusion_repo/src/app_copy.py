import os


def main():
    # read the config path from the environment
    path = os.environ.get("APP_CONFIG", "app.toml")
    return path


if __name__ == "__main__":
    main()
