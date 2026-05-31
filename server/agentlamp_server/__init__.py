"""AgentLamp local frame server package.

Serves the device frame API contract (see docs/api/device_frame_api.md):

    GET /api/v1/device/{device_id}/frame
    Authorization: Bearer <device_token>

Local mode binds 0.0.0.0:8787 over plain HTTP on the LAN. The firmware appends
the path to FRAME_BASE_URL (http://<lan-ip>:8787).
"""

__version__ = "0.1.0"
