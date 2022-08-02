#!/usr/bin/env python2

# Rekall Memory Forensics
# Copyright 2016 Google Inc. All Rights Reserved.
#
# Author: Michael Cohen scudette@google.com
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
#

__author__ = "Michael Cohen <scudette@google.com>"

"""This plugin implements the config_updater initialization tool.
"""
import time
import os
import yaml

from rekall import plugin
from rekall_lib import yaml_utils
from rekall_agent import crypto

from rekall_agent.config import agent
from rekall_agent.client_actions import interrogate
from rekall_agent.locations import cloud
from rekall_agent.policies import gcs
from rekall_agent.servers import http_server


class AgentServerInitialize(plugin.TypedProfileCommand, plugin.Command):
    """The base config initialization plugin.

    Depending on the server deployment type different initialization plugins can
    be implemented.
    """
    __abstract = True

    PHYSICAL_AS_REQUIRED = False
    PROFILE_REQUIRED = False

    __args = [
        dict(name="config_dir", positional=True, required=True,
             help="The directory to write configuration files into."),

        dict(name="client_writeback_path",
             default="/etc/rekall/agent.local.json",
             help="Path to the local client writeback location"),

        dict(name="labels", type="Array", default=["All"],
             help="The list of labels."),
    ]

    table_header = [
        dict(name="Message")
    ]

    ca_private_key_filename = "ca.private_key.pem"
    ca_cert_filename = "ca.cert.pem"
    server_private_key_filename = "server.private_key.pem"
    server_certificate_filename = "server.certificate.pem"
    client_config_filename = "client.config.yaml"
    server_config_filename = "server.config.yaml"
    secret = os.urandom(5).encode("hex")
    client_config_warning = ("# Warning: Do not edit this file. "
                             "Edit the server config instead.\n")

    # We will generate or read these from existing config.
    ca_cert = server_cert = server_private_key = None


    def generate_keys(self):
        """Generates various keys if needed."""
        ca_private_key_filename = os.path.join(
            self.config_dir, self.ca_private_key_filename)

        ca_cert_filename = os.path.join(
            self.config_dir, self.ca_cert_filename)

        try:
            ca_private_key = crypto.RSAPrivateKey.from_primitive(open(
                ca_private_key_filename).read(), session=self.session)

            self.ca_cert = crypto.X509Ceritifcate.from_primitive(open(
                ca_cert_filename).read(), session=self.session)

            yield dict(Message=f"Reusing existing CA keys in {ca_cert_filename}")
        except IOError:
            yield dict(
                Message=f"Generating new CA private key into {ca_private_key_filename} and {ca_cert_filename}"
            )

            ca_private_key = crypto.RSAPrivateKey(
                session=self.session).generate_key()

            with open(ca_private_key_filename, "wb") as fd:
                fd.write(ca_private_key.to_primitive())

            self.ca_cert = crypto.MakeCACert(ca_private_key,
                                             session=self.session)
            with open(ca_cert_filename, "wb") as fd:
                fd.write(self.ca_cert.to_primitive())

        # Now same thing with the server keys.
        server_private_key_filename = os.path.join(
            self.config_dir, self.server_private_key_filename)

        server_certificate_filename = os.path.join(
            self.config_dir, self.server_certificate_filename)

        try:
            self.server_private_key = crypto.RSAPrivateKey.from_primitive(open(
                server_private_key_filename).read(), session=self.session)

            self.server_cert = crypto.X509Ceritifcate.from_primitive(open(
                server_certificate_filename).read(), session=self.session)

            yield dict(
                Message=f"Reusing existing server keys in {server_certificate_filename}"
            )

        except IOError:
            yield dict(
                Message=f"Generating new Server private keys into {server_private_key_filename} and {server_certificate_filename}"
            )

            self.server_private_key = crypto.RSAPrivateKey(
                session=self.session).generate_key()

            with open(server_private_key_filename, "wb") as fd:
                fd.write(self.server_private_key.to_primitive())

            self.server_cert = crypto.MakeCASignedCert(
                unicode("Rekall Agent Server"),
                self.server_private_key,
                self.ca_cert,
                ca_private_key,
                session=self.session)

            with open(server_certificate_filename, "wb") as fd:
                fd.write(self.server_cert.to_primitive())

        # Ensure the keys verify before we write them.
        self.server_cert.verify(self.ca_cert.get_public_key())

    def _build_config(self, config):
        # Config should already be populated with the server and
        # client policies.
        config.ca_certificate = self.ca_cert
        labels = self.plugin_args.labels
        if "All" not in labels:
            labels.add("All")

        config.server.certificate = self.server_cert
        config.server.private_key = self.server_private_key

        config.client.labels = labels
        config.client.secret = self.secret
        config.client.writeback_path = self.plugin_args.client_writeback_path

        config.manifest = agent.Manifest.from_keywords(
            session=self.session,

            rekall_session=dict(live="API"),

            # When the client starts up we want it to run the startup action and
            # store the result in the Startup batch queue.
            startup_actions=[
                interrogate.StartupAction.from_keywords(
                    session=self.session,
                    startup_message=(
                        interrogate.Startup.from_keywords(
                            session=self.session,
                            location=config.server.flow_ticket_for_client(
                                "Startup", path_template="{client_id}",
                                expiration=time.time() + 60 * 60  * 24 * 365,
                            )
                        )
                    )
                )
            ]
        )

        # Now create a signed manifest.
        config.signed_manifest = agent.SignedManifest.from_keywords(
            session=self.session,
            data=config.manifest.to_json(),
            server_certificate=config.server.certificate,
        )

        config.signed_manifest.signature = (
            config.server.private_key.sign(
                config.signed_manifest.data))

    def write_config(self):
        server_config_filename = os.path.join(
            self.config_dir, self.server_config_filename)

        if os.access(server_config_filename, os.R_OK):
            yield dict(
                Message=f"Server config at {server_config_filename} exists. Remove to regenerate."
            )


            # Load existing server config.
            server_config_data = open(server_config_filename, "rb").read()
            config = agent.Configuration.from_primitive(
                yaml.safe_load(server_config_data), session=self.session)

        else:
            # Make a new configuration
            config = agent.Configuration(session=self.session)
            self.session.SetParameter("agent_config_obj", config)

            self._build_config(config)

            yield dict(Message=f"Writing server config file {server_config_filename}")

            with open(server_config_filename, "wb") as fd:
                fd.write(yaml_utils.safe_dump(config.to_primitive()))

        # The client gets just the client part of the configuration.
        client_config = agent.Configuration(session=self.session)
        client_config.client = config.client
        client_config.ca_certificate = config.ca_certificate

        client_config_filename = os.path.join(
            self.config_dir, self.client_config_filename)

        yield dict(Message=f"Writing client config file {client_config_filename}")

        with open(client_config_filename, "wb") as fd:
            fd.write(self.client_config_warning +
                     yaml_utils.safe_dump(client_config.to_primitive()))

        # Now load the server config file to make sure it is validly written.
        self.session.SetParameter("agent_configuration", server_config_filename)
        self._config = self.session.GetParameter(
            "agent_config_obj", cached=False)

        if self._config is None:
            raise RuntimeError("Unable to parse provided configuration.")

    def write_manifest(self):
        yield dict(Message="Writing manifest file.")

        # Now upload the signed manifest to the bucket. Manifest must be
        # publicly accessible.
        upload_location = self._config.server.manifest_for_server()
        yield dict(Message="Writing manifest file to %s" % (
            upload_location.to_path()))

        upload_location.write_file(self._config.signed_manifest.to_json())

        print yaml_utils.safe_dump(self._config.manifest.to_primitive())


    def collect(self):
        """This should be an interactive script."""
        self.config_dir = self.plugin_args.config_dir
        if not os.access(self.config_dir, os.R_OK):
            raise plugin.PluginError(
                f"Unable to write to config directory {self.config_dir}"
            )


        for method in [self.generate_keys,
                       self.write_config,
                       self.write_manifest]:
            yield from method()
        yield dict(Message="Done!")


class AgentServerInitializeGCS(AgentServerInitialize):
    """Initialize the agent server to work in Google Cloud Storage."""

    name = "agent_server_initialize_gcs"

    __args = [
        dict(name="bucket", required=True,
             help="The bucket name for the GCS deployment."),

        dict(name="service_account_path", required=True,
             help="Path to the service account (JSON) credentials"),
    ]

    def _build_config(self, config):
        service_account = cloud.ServiceAccount.from_json(
            open(self.plugin_args.service_account_path, "rb").read(),
            session=self.session)

        config.server = gcs.GCSServerPolicy.from_keywords(
            session=self.session,
            bucket=self.plugin_args.bucket,
            service_account=service_account,
        )

        config.client = gcs.GCSAgentPolicy.from_keywords(
            session=self.session,
            manifest_location=config.server.manifest_for_client()
        )

        super(AgentServerInitializeGCS, self)._build_config(config)


class AgentServerInitializeLocalHTTP(AgentServerInitialize):
    """Initialize the agent server to work in Google Cloud Storage."""

    name = "agent_server_initialize_http"

    __args = [
        dict(name="base_url",
             help="The publicly accessible URL of the frontend."),

        dict(name="bind_port", type="IntParser", default=8000,
             help="Port to bind to"),

        dict(name="bind_address", default="127.0.0.1",
             help="Address to bind to"),
    ]

    def _build_config(self, config):
        config.server = http_server.HTTPServerPolicy.from_keywords(
            session=self.session,
            base_url=self.plugin_args.base_url,
            bind_port=self.plugin_args.bind_port,
            bind_address=self.plugin_args.bind_address,
            certificate=self.server_cert,
            private_key=self.server_private_key,
        )
        config.client = http_location.HTTPClientPolicy.from_keywords(
            session=self.session,
            manifest_location=config.server.manifest_for_client()
        )

        super(AgentServerInitializeLocalHTTP, self)._build_config(config)

    def write_manifest(self):
        """Ignore uploading the manifest.

        For HTTP server we never write the manifest because it is
        served as an API action. Unlike the GCS case we can not write
        the manifest before the server configuration is complete and
        the server is started. So for HTTP servers we write the
        manifest into the configuration and just serve from there.
        """
        return []
