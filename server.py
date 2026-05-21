import os
import uuid
import json
from livekit import api
from livekit.api import LiveKitAPI, ListRoomsRequest
from livekit.protocol.sip import CreateSIPParticipantRequest
from livekit.api import CreateAgentDispatchRequest
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from flask_cors import CORS
from supabase import create_client
from prompts import INSTRUCTIONS, WELCOME_MESSAGE

load_dotenv()

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        response = app.make_default_options_response()
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        return response

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {
        "tts_voice": "en-US-Standard-C",
        "tts_speed": 1.0,
        "instructions": INSTRUCTIONS,
        "welcome_message": WELCOME_MESSAGE,
        "lead_data": {
            "lead_name": "",
            "lead_email": "",
            "lead_phone": "",
            "lead_time_zone": "UTC",
        },
        "agent_settings_web": {
            "aai_eot_confidence":   0.6,
            "aai_min_turn_silence": 160,
            "aai_max_turn_silence": 2400,
            "aai_vad_threshold":    0.4,
            "aai_format_turns":     True,
        },
        "agent_settings_sip": {
            "aai_eot_confidence":   0.65,
            "aai_min_turn_silence": 200,
            "aai_max_turn_silence": 2000,
            "aai_vad_threshold":    0.5,
            "aai_format_turns":     True,
        },
    }

def save_config(data):
    config = load_config()
    allowed = {"tts_voice", "tts_speed", "instructions", "welcome_message", "lead_data", "agent_settings_web", "agent_settings_sip"}
    config.update({k: v for k, v in data.items() if k in allowed})
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    return config


def get_supabase_service():
    return create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_KEY")
    )


def verify_admin(req):
    token = req.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        raise PermissionError("No token provided")
    anon = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_ANON_KEY"))
    caller = anon.auth.get_user(token)
    if not caller or not caller.user:
        raise PermissionError("Invalid token")
    sb = get_supabase_service()
    profile = sb.table("profiles").select("role").eq("id", caller.user.id).single().execute()
    if not profile.data or profile.data.get("role") != "admin":
        raise PermissionError("Not admin")
    return caller.user.id


async def generate_room_name():
    name = "room-" + str(uuid.uuid4())[:8]
    rooms = await get_rooms()
    while name in rooms:
        name = "room-" + str(uuid.uuid4())[:8]
    return name


async def get_rooms():
    lk_api = LiveKitAPI()
    rooms = await lk_api.room.list_rooms(ListRoomsRequest())
    await lk_api.aclose()
    return [room.name for room in rooms.rooms]


@app.route("/config", methods=["GET"])
def get_config():
    return jsonify(load_config())


@app.route("/config", methods=["POST"])
def post_config():
    data = request.get_json()
    config = save_config(data)
    return jsonify({"status": "saved", "config": config})


@app.route("/getToken")
async def get_token():
    name = request.args.get("name", "user")
    room = request.args.get("room", None)

    if not room:
        room = await generate_room_name()

    token = api.AccessToken(os.getenv("LIVEKIT_API_KEY"), os.getenv("LIVEKIT_API_SECRET")) \
        .with_identity(name) \
        .with_name(name) \
        .with_grants(api.VideoGrants(
            room_join=True,
            room=room
        ))

    # Dispatch the agent to this room so it picks up the web call
    lk = LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )
    try:
        await lk.agent_dispatch.create_dispatch(
            CreateAgentDispatchRequest(
                agent_name="iklipse-dialer",
                room=room,
            )
        )
    finally:
        await lk.aclose()

    return jsonify({"token": token.to_jwt(), "room": room})


@app.route("/call", methods=["POST"])
async def outbound_call():
    """
    Trigger an outbound SIP call. Lead data in the payload overrides config.json for this call only.

    Expected JSON body:
    {
        "to": "+1234567890",           -- REQUIRED: E.164 number to call
        "lead_name": "John",           -- optional
        "lead_email": "j@example.com", -- optional
        "lead_phone": "+1234567890",   -- optional (defaults to 'to')
        "lead_time_zone": "Africa/Cairo", -- optional (defaults to config)
    }
    """
    data = request.get_json()

    to_number = data.get("to")
    if not to_number:
        return jsonify({"error": "Missing 'to' phone number"}), 400

    trunk_id = os.getenv("LIVEKIT_SIP_TRUNK_ID")
    if not trunk_id:
        return jsonify({"error": "LIVEKIT_SIP_TRUNK_ID not configured"}), 500

    # Build lead data for this call — merge with config defaults
    base_config = load_config()
    base_lead = base_config.get("lead_data", {})

    lead_data = {
        "lead_name":      data.get("lead_name")      or base_lead.get("lead_name", ""),
        "lead_email":     data.get("lead_email")     or base_lead.get("lead_email", ""),
        "lead_phone":     data.get("lead_phone")     or to_number,
        "lead_time_zone": data.get("lead_time_zone") or base_lead.get("lead_time_zone", "UTC"),
    }

    room_name = f"call-{uuid.uuid4().hex[:8]}"

    # Write per-room lead data so the agent can pick it up
    ROOMS_DIR = os.path.join(os.path.dirname(__file__), "rooms")
    os.makedirs(ROOMS_DIR, exist_ok=True)
    room_file = os.path.join(ROOMS_DIR, f"{room_name}.json")
    with open(room_file, "w", encoding="utf-8") as f:
        json.dump(lead_data, f, ensure_ascii=False)

    lk = LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )

    try:
        await lk.agent_dispatch.create_dispatch(
            CreateAgentDispatchRequest(
                agent_name="iklipse-dialer",
                room=room_name,
            )
        )

        participant = await lk.sip.create_sip_participant(
            CreateSIPParticipantRequest(
                sip_trunk_id=trunk_id,
                sip_call_to=to_number,
                sip_number=os.getenv("TWILIO_PHONE_NUMBER"),
                room_name=room_name,
                participant_identity=f"sip_{to_number}",
                participant_name=lead_data["lead_name"] or "Lead",
                play_dialtone=True,
                wait_until_answered=False,
            )
        )
    finally:
        await lk.aclose()

    return jsonify({
        "status": "calling",
        "room": room_name,
        "to": to_number,
        "lead": lead_data,
    })


@app.route("/admin/create-user", methods=["POST"])
def create_user():
    try:
        verify_admin(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except Exception:
        return jsonify({"error": "Auth failed"}), 401
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")
    full_name = data.get("full_name", "")
    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400
    try:
        sb = get_supabase_service()
        result = sb.auth.admin.create_user({
            "email": email,
            "password": password,
            "user_metadata": {"full_name": full_name},
            "email_confirm": True,
        })
        # Upsert profile row in case trigger didn't fire
        sb.table("profiles").upsert({
            "id": result.user.id,
            "email": email,
            "full_name": full_name,
            "role": "user",
            "credits_remaining": 100,
            "is_active": True,
        }).execute()
        return jsonify({"status": "created", "user_id": result.user.id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/delete-user", methods=["POST"])
def delete_user():
    try:
        caller_id = verify_admin(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except Exception:
        return jsonify({"error": "Auth failed"}), 401
    data = request.get_json()
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    if user_id == caller_id:
        return jsonify({"error": "Cannot delete your own account"}), 400
    try:
        sb = get_supabase_service()
        sb.auth.admin.delete_user(user_id)
        return jsonify({"status": "deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/update-credits", methods=["POST"])
def update_credits():
    try:
        verify_admin(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except Exception:
        return jsonify({"error": "Auth failed"}), 401
    data = request.get_json()
    user_id = data.get("user_id")
    credits = data.get("credits")
    if not user_id or credits is None:
        return jsonify({"error": "user_id and credits required"}), 400
    try:
        sb = get_supabase_service()
        sb.table("profiles").update({"credits_remaining": credits}).eq("id", user_id).execute()
        return jsonify({"status": "updated", "credits": credits})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/all-agents", methods=["GET"])
def get_all_agents():
    try:
        verify_admin(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except Exception:
        return jsonify({"error": "Auth failed"}), 401
    try:
        sb = get_supabase_service()
        result = sb.table("agents").select(
            "*, profiles(email, full_name)"
        ).order("created_at", desc=True).execute()
        return jsonify(result.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/all-calls", methods=["GET"])
def get_all_calls():
    try:
        verify_admin(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except Exception:
        return jsonify({"error": "Auth failed"}), 401
    try:
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
        sb = get_supabase_service()
        result = sb.table("calls").select(
            "*, profiles(email, full_name), agents(name)"
        ).order("started_at", desc=True).limit(limit).offset(offset).execute()
        return jsonify(result.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/all-integrations", methods=["GET"])
def get_all_integrations():
    try:
        verify_admin(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except Exception:
        return jsonify({"error": "Auth failed"}), 401
    try:
        sb = get_supabase_service()
        result = sb.table("integrations").select(
            "*, profiles(email, full_name)"
        ).order("created_at", desc=True).execute()
        return jsonify(result.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
