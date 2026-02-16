#!/usr/bin/env python3
"""
Codex WebSocket Client

This script connects to the Codex WebSocket server and receives
real-time streaming output from the Codex CLI.

Usage:
    # Create virtual environment and install dependencies with uv
    uv venv codex_ws_client
    source codex_ws_client/bin/activate
    pip install websockets

    # Run the client
    python python_websocket_client.py

Or with uv directly:
    uv run python_websocket_client.py
"""

import asyncio
import websockets
import argparse
import sys
from typing import Optional


async def connect_and_stream(
    uri: str = "ws://127.0.0.1:8765",
    reconnect: bool = True,
    reconnect_delay: float = 2.0,
):
    """
    Connect to Codex WebSocket server and stream output.

    Args:
        uri: WebSocket server URI (default: ws://127.0.0.1:8765)
        reconnect: Whether to reconnect on disconnect (default: True)
        reconnect_delay: Delay between reconnection attempts in seconds (default: 2.0)
    """
    print(f"Connecting to Codex WebSocket at {uri}...")
    print("Press Ctrl+C to exit\n")

    while True:
        try:
            async with websockets.connect(uri) as ws:
                print("✓ Connected to Codex WebSocket!")
                print("=" * 50)

                buffer = ""

                async for message in ws:
                    try:
                        text = message if isinstance(message, str) else message.decode('utf-8')

                        # Accumulate text and print in real-time
                        buffer += text

                        # Print each character as it arrives for real-time effect
                        print(text, end='', flush=True)

                        # Optional: Log to file
                        # with open('codex_output.txt', 'a') as f:
                        #     f.write(text)

                    except Exception as e:
                        print(f"\n\nError processing message: {e}", file=sys.stderr)

                print("\n" + "=" * 50)
                print("Connection closed")

                if not reconnect:
                    break

        except websockets.exceptions.ConnectionRefused:
            print(f"\n✗ Could not connect to {uri}")
            print(f"  Make sure Codex is running with WebSocket streaming enabled")
            print(f"  See codex-rs/tui/src/chatwidget.rs for details")

            if not reconnect:
                return 1

        except KeyboardInterrupt:
            print("\n\nShutting down...")
            break

        except Exception as e:
            print(f"\n\nError: {e}", file=sys.stderr)

            if not reconnect:
                return 1

        if reconnect:
            print(f"\nReconnecting in {reconnect_delay} seconds...")
            await asyncio.sleep(reconnect_delay)

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Connect to Codex WebSocket and stream output in real-time"
    )
    parser.add_argument(
        "--uri",
        "-u",
        default="ws://127.0.0.1:8765",
        help="WebSocket server URI (default: ws://127.0.0.1:8765)",
    )
    parser.add_argument(
        "--no-reconnect",
        action="store_true",
        help="Don't automatically reconnect on disconnect",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=2.0,
        help="Delay between reconnection attempts in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose output",
    )

    args = parser.parse_args()

    if args.verbose:
        import logging
        logging.basicConfig(level=logging.INFO)
        websockets.logger.setLevel(logging.INFO)

    try:
        exit_code = asyncio.run(connect_and_stream(
            uri=args.uri,
            reconnect=not args.no_reconnect,
            reconnect_delay=args.reconnect_delay,
        ))
        sys.exit(exit_code)

    except KeyboardInterrupt:
        print("\nShutdown by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
