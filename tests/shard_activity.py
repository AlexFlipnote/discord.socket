import asyncio
import os

from discord_http import http


async def main():
    while True:
        r = await http.query(
            "GET",
            "http://127.0.0.1:8080/shards",
            res_method="json"
        )

        # Only print the last 10 shards
        print("\n".join([
            f"{k.ljust(5)} {v['activity']['between'].ljust(17)} {v['activity']['last']}"
            for k, v in sorted(
                r.response.items(),
                key=lambda x: x[1]["activity"]["between"]
            )
        ][-25:]))

        # Delete the 10 lines to keep the output clean
        await asyncio.sleep(1)
        os.system("cls" if os.name == "nt" else "clear")

try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
