from discord_http import Client, Message
from discord_socket import SocketClient, Intents


client = Client(
    token="BOT_TOKEN",
    application_id=1337,
    public_key="PUBLIC_KEY"
)

socket = SocketClient(
    bot=client,  # Pass the client to the socket
    intents=(
        Intents.guilds |
        Intents.guild_messages
    )
)


@client.listener()
async def on_message_create(msg: Message):
    print(msg)


socket.start()  # Starts the socket, runs in a separate task
client.start(host="127.0.0.1", port=8080)  # Then start the client
