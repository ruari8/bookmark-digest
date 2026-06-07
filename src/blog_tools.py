"""
Safe Bird helper commands for the Codex blog writer.

These commands read local cookies from config/cookies.json and print sanitized
tweet context. They never print cookie values.
"""

import argparse
import json

from .blog import BirdContextFetcher, CONFIG_DIR, parse_tweet_context


def _tweet_to_dict(tweet):
    data = {
        "id": tweet.id,
        "author_username": tweet.author_username,
        "author_name": tweet.author_name,
        "created_at": tweet.created_at,
        "tweet_url": tweet.tweet_url,
        "text": tweet.text,
        "urls": tweet.urls,
        "media": [item.__dict__ for item in tweet.media],
        "in_reply_to_id": tweet.in_reply_to_id,
    }
    if tweet.quote_tweet:
        data["quote_tweet"] = _tweet_to_dict(tweet.quote_tweet)
    return data


def main():
    parser = argparse.ArgumentParser(description="Fetch sanitized X context through Bird")
    parser.add_argument("command", choices=["read", "thread", "replies"])
    parser.add_argument("tweet_id")
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()

    fetcher = BirdContextFetcher(CONFIG_DIR / "cookies.json")
    if args.command == "read":
        tweet = fetcher.read_tweet(args.tweet_id)
        payload = _tweet_to_dict(tweet) if tweet else {}
    elif args.command == "thread":
        payload = [_tweet_to_dict(tweet) for tweet in fetcher.thread(args.tweet_id, limit=args.limit)]
    else:
        payload = [_tweet_to_dict(tweet) for tweet in fetcher.replies(args.tweet_id, limit=args.limit)]

    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
