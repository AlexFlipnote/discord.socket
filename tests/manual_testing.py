import json
import logging

from discord_http import Client, Message, PartialMessage
from discord_socket import SocketClient, Intents

with open("./config.json", "r") as f:
    config = json.load(f)


client = Client(
    token=config["token"],
    application_id=config["application_id"],
    public_key=config["public_key"],
    logging_level=logging.DEBUG
)

socket = SocketClient(
    bot=client,
    intents=Intents.from_names(
        "guilds",
        "guild_messages",
        "guild_message_reactions",
        "guild_bans",
        "guild_emojis_and_stickers",
        "direct_messages",
    )
)


@client.listener()
async def on_message_create(msg: Message):
    print((msg, repr(msg)))


@client.listener()
async def on_message_update(msg: Message):
    print((msg, repr(msg)))


@client.listener()
async def on_message_delete(msg: PartialMessage):
    print(msg)


@client.listener()
async def on_message_reaction_add(data: dict):
    print(data)


@client.listener()
async def on_message_reaction_remove(data: dict):
    print(data)


socket.start()
client.start(host=config["host"], port=config["port"])
