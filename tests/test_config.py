from app.config import Settings
def test_readiness_hides_values():
    assert Settings(neuron_api_key="secret").readiness() == {"neuron":True,"github":False,"linear":False}
def test_iteration_validation():
    try: Settings(agent_max_iterations=0)
    except ValueError: return
    assert False
