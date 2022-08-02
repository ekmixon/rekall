import argparse
import logging
import yaml

from rekall_agent import agent
from rekall_agent import common
from rekall import plugins
from rekall import session
from rekall import yaml_utils


parser = argparse.ArgumentParser(description='Rekall Agent Pool Client')
parser.add_argument('config', help='configuration file.')

parser.add_argument('--state_dir', default="/tmp/",
                    help='Where per client state is stored.')

parser.add_argument('--number', default=10, type=int,
                    help='Total number of clients to run.')

parser.add_argument('--verbose', action="store_true",
                    help='Total number of clients to run.')


def launch_client(_):
    flags, client_number = _
    config = yaml.safe_load(open(flags.config).read())
    config["client"][
        "writeback_path"
    ] = f"{flags.state_dir}/pool_writeback{client_number}.yaml"

    config_file_name = f"{flags.state_dir}/pool_config{client_number}.yaml"

    with open(config_file_name, "wb") as fd:
        fd.write(yaml_utils.safe_dump(config))

    rekall_session = session.Session(agent_configuration=config_file_name)
    agent_plugin = agent.RekallAgent(
        session=rekall_session,
    )

    # This does not exit.
    agent_plugin.collect()


if __name__ == '__main__':
    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(10)

    workers = common.LoggingPool(args.number + 10)
    workers.map(
        launch_client,
        [(args, i) for i in range(args.number)])
