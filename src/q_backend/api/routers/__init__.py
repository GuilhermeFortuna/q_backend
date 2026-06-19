"""Per-domain FastAPI routers for the QuantLauncher API.

Conventions (follow for every new domain router):
- One ``APIRouter`` per domain, exported as ``router``.
- Set ``tags=["<domain>"]`` on the router.
- Preserve exact original URL paths on each route decorator — do not use
  ``prefix=`` rewrites that change the public path.
- Request/response models live in ``q_backend.api.schemas.<domain>``.
- Domain logic belongs in an existing domain package (``market_data/``,
  ``backtesting/``, etc.) or in ``api/services/<domain>.py`` when API-specific.
- Obtain the shared ``market_data_service`` via ``Depends(get_market_data_service)``
  from ``q_backend.api.dependencies``; never instantiate ``MarketDataService`` in a router.
- Keep handlers thin: validate input, call a service, map the result to a schema.
"""
