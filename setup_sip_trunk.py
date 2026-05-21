"""
Run this ONCE to register the Twilio SIP outbound trunk in LiveKit.
It prints the trunk ID — save it in your .env as LIVEKIT_SIP_TRUNK_ID.

Usage:
    python setup_sip_trunk.py
"""
import asyncio
import os
from dotenv import load_dotenv
from livekit.api import LiveKitAPI
from livekit.protocol.sip import CreateSIPOutboundTrunkRequest, SIPOutboundTrunkInfo

load_dotenv()


async def main():
    lk = LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )

    trunk = await lk.sip.create_outbound_trunk(
        CreateSIPOutboundTrunkRequest(
            trunk=SIPOutboundTrunkInfo(
                name="Twilio Outbound",
                address="iklipse-retell-voice-agent.pstn.twilio.com",
                numbers=["+17752433773"],
                auth_username=os.getenv("TWILIO_ACCOUNT_SID"),
                auth_password=os.getenv("TWILIO_AUTH_TOKEN"),
            )
        )
    )

    await lk.aclose()
    print(f"\n✅ SIP Trunk created successfully!")
    print(f"   Trunk ID: {trunk.sip_trunk_id}")
    print(f"\n👉 Add this to your backend/.env:")
    print(f"   LIVEKIT_SIP_TRUNK_ID={trunk.sip_trunk_id}")


if __name__ == "__main__":
    asyncio.run(main())
