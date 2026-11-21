# Oval Warden

Oval Warden is Urban-Ers’ Python entry for the 2026 Builders Buildathon Mech Royale track. It prioritizes placement. It uses the supplied arena geometry to route around buildings, forecasts the shrinking zone, and seeks power cubes and supply drops. When taking a fight, it leads moving targets, accounts for torso turn speed, and scores movement options for nearby threats and projectiles.

## Build and check

Download and extract the starter kit from the [Mech Royale track page](https://www.joinbuilders.org/buildathon/tracks/builders). With the extracted kit next to this repository, run these commands in Bash from the repository root.

```sh
python3 ../mechfight-kit/check/check.py -- python3 -B -u bot.py
docker build --platform linux/amd64 -t oval-warden .
python3 ../mechfight-kit/check/check.py --map-dir /arena -- \
  docker run --rm -i --network none --cpus 1 --memory 512m --read-only \
  --tmpfs /tmp:rw,size=64m --user 65534:65534 \
  -v "$PWD/../mechfight-kit/arena:/arena:ro" oval-warden
```

The replay check confirms that the bot speaks the protocol and responds in time. The official practice run measures placement over 48 scored matches.

## Submit

Put these files at the root of a **public** GitHub repository and submit its URL on the [Mech Royale track page](https://www.joinbuilders.org/buildathon/tracks/builders) while signed in as Urban-Ers. Keep the repository and submitted commit public through the final results. Submissions close Sunday, November 22 at 11:00 a.m. ET, which is Monday, November 23 at 3:00 a.m. AEDT.

## Credits

`mechfight.py` is the JSON-lines wrapper from the official Builders starter kit. The entry uses Python’s standard library and the map mounted by the organizer. See [LICENSE](LICENSE) for the MIT terms.
