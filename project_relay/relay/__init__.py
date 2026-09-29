"""
Project Relay V2: extension-driven autonomous relay.

    ChatGPT tab (user's real Chrome) + Project Relay extension
        <-> prelayd (HTTP on 127.0.0.1, token auth)
        <-> SQLite (authority) / local Bash runner / watchdog

The extension never executes commands. The daemon never touches
the ChatGPT page. SQLite state decides every step.
"""
