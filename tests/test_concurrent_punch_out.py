
import asyncio
import httpx

URL = "http://127.0.0.1:8000/attendance/punch-out"
PAYLOAD = {"emp_code": "EMP1005"}

async def send_request(client):
    response = await client.post(URL, json=PAYLOAD)
    return response.status_code, response.json()

async def main():
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            send_request(client),
            send_request(client)
        )

    for status_code, body in results:
        print(status_code, body)

asyncio.run(main())
