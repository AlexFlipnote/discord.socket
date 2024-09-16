import json
import logging

from discord_http import Client, Message, PartialMessage
from discord_socket import SocketClient, Intents

with open("./s.config.json", "r") as f:
    config = json.load(f)


client = Client(
    token=config["token"],
    application_id=config["application_id"],
    public_key=config["public_key"],
    logging_level=logging.DEBUG
)

socket = SocketClient(
    bot=client,
    intents=(
        Intents.guilds |
        Intents.guild_messages |
        Intents.guild_message_reactions |
        Intents.guild_bans |
        Intents.guild_emojis_and_stickers |
        Intents.direct_messages
    )
)


"""@client.listener()
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
    print(data)"""


socket.start()
client.start(host=config["host"], port=config["port"])
