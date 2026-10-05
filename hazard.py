"""
Hazard webhook endpoint (Flask).

On the Pi, this Flask app listens for incoming hazards from Slack Code or Stream Deck,
applies them to the robot, and the control loop reacts.

On Mac, this is not used (testing is done by editing params.json directly).
When the Pi is live, hazards come via:
- Slack workflow → webhook on the Pi
- Stream Deck → Slack workflow → webhook on the Pi
"""

try:
    from flask import Flask, request, jsonify
except ImportError:
    Flask = None


if Flask:
    app = Flask(__name__)

    # Module-level reference to the robot (set at startup).
    _robot = None

    def set_robot(robot):
        """Set the robot instance. Called from control_loop.py at startup."""
        global _robot
        _robot = robot

    @app.route("/hazard", methods=["POST"])
    def receive_hazard():
        """
        Receive a hazard event.

        Expected JSON:
        {
            "hazard": "fog" | "rain" | "rush_hour" | "gremlin" | "blackout"
        }
        """
        if not _robot:
            return jsonify({"error": "robot not initialized"}), 500

        try:
            data = request.get_json() or {}
            hazard_name = data.get("hazard", "").lower()

            if not hazard_name or hazard_name not in [
                "fog",
                "rain",
                "rush_hour",
                "gremlin",
                "blackout",
            ]:
                return jsonify({"error": f"unknown hazard: {hazard_name}"}), 400

            _robot.apply_hazard(hazard_name)
            return jsonify({"status": "ok", "hazard": hazard_name}), 200

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    def run_server(host: str = "0.0.0.0", port: int = 5000):
        """Start the Flask server (blocking)."""
        app.run(host=host, port=port, debug=False)

else:
    # Flask not available (e.g., on Mac without the dependency).
    app = None

    def set_robot(robot):
        pass

    def run_server(host: str = "0.0.0.0", port: int = 5000):
        raise RuntimeError("Flask not installed. This endpoint requires: pip install flask")


if __name__ == "__main__":
    # Quick test: start the server
    if app:
        print("Starting hazard webhook server on http://0.0.0.0:5000")
        run_server()
    else:
        print("Flask not available. Install it with: pip install flask")
