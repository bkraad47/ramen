from ramen_runtime import InputRequired, elicit_url, input_responses, text


def run():
    if "signin" not in input_responses():
        raise InputRequired({"signin": elicit_url("Sign in to continue", "https://auth.example/start")})
    return text("signed in")
