from app.config import Settings
def test_readiness_hides_values():
    assert Settings(neuron_api_key="secret", github_token="", linear_api_key="", linear_client_id="", linear_client_secret="").readiness() == {"neuron":True,"github":False,"linear":False,"linear_oauth":False}
def test_iteration_validation():
    try: Settings(agent_max_iterations=0)
    except ValueError: return
    assert False
