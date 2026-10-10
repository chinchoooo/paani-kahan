"""The world layer: tiles, data providers, caches, rain, scheduling.

See ARCHITECTURE.md for the design. In short: the map is cut into Web-Mercator tiles at
two levels of detail. Each tile is fetched, built and forecast independently, by
priority, in the background; everything is cached on disk and served immutably.
"""
