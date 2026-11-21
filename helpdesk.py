"""A disposable help-desk application. All authenticated staff share one test queue."""
from flask import Flask, abort, jsonify, request


def create_app(db, sessions):
    app = Flask(__name__)
    answers = {}
    db.execute("CREATE TABLE notes (id TEXT PRIMARY KEY, customer TEXT, question TEXT UNIQUE, body TEXT)")

    @app.before_request
    def authenticate():
        if request.headers.get("Authorization") not in sessions:
            abort(403)

    @app.get("/desk/answer")
    def answer():
        question = request.args["question"]
        if question not in answers:
            row = db.execute("SELECT body FROM notes WHERE question = ?", (question,)).fetchone()
            answers[question] = row[0] if row else "No matching note."
        return jsonify(caller=sessions[request.headers["Authorization"]], conversation="desk",
                       route=request.path, question=question, text=answers[question])

    @app.delete("/desk/customers/<customer>/notes")
    def delete_notes(customer):
        if sessions[request.headers["Authorization"]] != "desk-owner":
            abort(403)
        with db:
            questions = db.execute("SELECT question FROM notes WHERE customer = ?", (customer,)).fetchall()
            db.execute("DELETE FROM notes WHERE customer = ?", (customer,))
        return "", 204

    return app
