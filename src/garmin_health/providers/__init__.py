"""Provider packages: everything that knows how the data was acquired.

One subpackage per provider. A provider owns its acquisition strategy (a polling
scraper, a signed webhook, or a notification followed by a fetch), the store it
writes, and the reader over that store. Everything it hands upward is a
``health_data_service`` type or a stdlib type.

``garmindb`` is the only provider today. The selector that maps a configured
name to a provider belongs here rather than in ``app.py``; see
``provider_refactor.md`` Phase 4, which is also when the port it returns exists.
"""
