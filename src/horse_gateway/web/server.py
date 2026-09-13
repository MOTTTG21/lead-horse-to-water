"""Production entrypoint: `uvicorn horse_gateway.web.server:app`.

Builds the app with real dependencies (a real Anthropic client reading
ANTHROPIC_API_KEY from the environment, a real database via DATABASE_URL,
synthetic traffic running). Tests use `create_app(...)` directly with
fakes instead of importing this module.
"""

from dotenv import load_dotenv

from .app import create_app

load_dotenv()

app = create_app()
