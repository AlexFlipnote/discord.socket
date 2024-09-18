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
    # logging_level=logging.DEBUG
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


@client.listener()
async def on_message_create(msg: Message, shard_id: int):
    # This is some very scuffed testing, but for 190k servers, whatever...
    if msg.author.id != 86477779717066752:
        return

    print(f"Detected {msg.author} in shard {shard_id}")
    if msg.content.startswith("!ping"):
        await msg.reply(f"Pong in shard {shard_id}")


"""@client.listener()
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
