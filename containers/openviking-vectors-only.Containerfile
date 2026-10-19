FROM ghcr.io/volcengine/openviking@sha256:2c104187d17d9b5c837d578990ae574807ad8ab78ba84191ddafabfde207af38

# Inherit the base image's empty User (root), command, entrypoint, labels, and license unchanged.
COPY containers/openviking-vectors-only.patch.json /tmp/openviking-vectors-only.patch.json
COPY containers/apply-openviking-copy-patch.py /tmp/apply-openviking-copy-patch.py
RUN python3 /tmp/apply-openviking-copy-patch.py /tmp/openviking-vectors-only.patch.json \
    && rm -f /tmp/openviking-vectors-only.patch.json /tmp/apply-openviking-copy-patch.py
