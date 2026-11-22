# Oval Warden

Oval Warden is Urban-Ers’ Python entry for the 2026 Builders Buildathon Mech Royale track. It prioritizes placement. It uses the supplied arena geometry to route around buildings, forecasts the shrinking zone, and seeks power cubes and supply drops. When taking a fight, it leads moving targets, accounts for torso turn speed, and scores movement options for nearby threats and projectiles.

Breakaway detects when a ground route stalls during a circle rotation and looks for accessible ground before replanning.

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

The replay check confirms that the bot speaks the protocol and responds in time. The official practice run measures placement over 96 matches with fresh random seeds for each submission.


## Credits

`mechfight.py` is the JSON-lines wrapper from the official Builders starter kit. The entry uses Python’s standard library and the map mounted by the organizer. See [LICENSE](LICENSE) for the MIT terms.
