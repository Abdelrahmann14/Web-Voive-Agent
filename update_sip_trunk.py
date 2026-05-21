"""
Run this to update the SIP trunk credentials in LiveKit.
"""
import asyncio
import os
from dotenv import load_dotenv
from livekit.api import LiveKitAPI
from livekit.protocol.sip import SIPOutboundTrunkInfo

load_dotenv()


async def main():
    lk = LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )

    trunk_id = os.getenv("LIVEKIT_SIP_TRUNK_ID")

    trunk = await lk.sip.update_outbound_trunk(
        trunk_id,
        SIPOutboundTrunkInfo(
            name="Twilio Outbound",
            address="iklipse-retell-voice-agent.pstn.twilio.com",
            numbers=["+17752433773"],
            auth_username="iklipse",
            auth_password="Iklipseworld12",
        )
    )

    await lk.aclose()
    print(f"SIP Trunk updated: {trunk.sip_trunk_id}")


if __name__ == "__main__":
    asyncio.run(main())
