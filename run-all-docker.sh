export COMPOSE_FILES="$(find . | grep "docker-compose.yaml")"

for COMPOSE_FILE in $COMPOSE_FILES
do
    docker compose -f $COMPOSE_FILE pull
    docker compose -f $COMPOSE_FILE up -d
done
