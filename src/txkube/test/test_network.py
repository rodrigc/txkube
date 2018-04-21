# Copyright Least Authority Enterprises.
# See LICENSE for details.

"""
Tests for ``txkube.network_kubernetes``.

See ``get_kubernetes`` for pre-requisites.
"""

from os import (
    environ,
    pathsep,
)
from os.path import (
    expanduser,
)
from inspect import (
    getcallargs,
)
from base64 import b64encode

from zope.interface import implementer
from zope.interface.verify import verifyClass

import attr

from yaml import safe_dump

from fixtures import TempDir

from testtools.matchers import AnyMatch, ContainsDict, Equals
from testtools.twistedsupport import succeeded

from eliot.testing import capture_logging

from OpenSSL.crypto import FILETYPE_PEM

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend

from twisted.test.proto_helpers import MemoryReactorClock
from twisted.trial.unittest import TestCase as TwistedTestCase

from twisted.python.filepath import FilePath
from twisted.python.url import URL
from twisted.python.components import proxyForInterface
from twisted.internet.defer import Deferred, succeed
from twisted.internet.ssl import (
    CertificateOptions, DN, KeyPair, trustRootFromCertificates,
)
from twisted.internet.interfaces import (
    IReactorSSL, IReactorTCP, IReactorTime, IReactorPluggableNameResolver,
)
from twisted.internet.endpoints import SSL4ServerEndpoint
from twisted.web.client import Agent
from twisted.web.server import Site
from twisted.web.resource import Resource
from twisted.web.static import Data

from ..testing import TestCase, cert
from ..testing.integration import kubernetes_client_tests

from .. import (
    IObject, v1, network_kubernetes, network_kubernetes_from_context,
)

from .._network import (
    _Memo,
    collection_location,
    _merge_configs_from_env,
)

from .._compat import native_string_to_bytes


def get_kubernetes(case):
    """
    Create a real ``IKubernetes`` provider, taking necessary
    configuration details from the environment.

    To use this set ``TXKUBE_INTEGRATION_CONTEXT`` to a context in your
    ``kubectl`` configuration.  Corresponding details about connecting to a
    cluster will be loaded from that configuration.
    """
    try:
        context = environ["TXKUBE_INTEGRATION_CONTEXT"]
    except KeyError:
        case.skipTest("Cannot find TXKUBE_INTEGRATION_CONTEXT in environment.")
    else:
        from twisted.internet import reactor
        return network_kubernetes_from_context(reactor, context)



class MergeConfigsTests(TestCase):
    """
    Tests for ``_merge_configs_from_env``.
    """
    def setUp(self):
        super(TestCase, self).setUp()
        self.configs = FilePath(self.useFixture(TempDir()).path)


    def test_first_value_wins(self):
        """
        The first configuration file to set a particular value "wins".  The
        resulting merged configuration object has that value regardless of any
        values which might be set by later configuration files.
        """
        a = self.configs.child(b"a")
        b = self.configs.child(b"b")
        yaml = safe_dump({
            u"apiVersion": u"v1",
            u"kind": u"Config",
            u"current-context": u"foo",
        })
        a.setContent(native_string_to_bytes(yaml))
        yaml = safe_dump({
            u"apiVersion": u"v1",
            u"kind": u"Config",
            u"current-context": u"bar",
        })
        b.setContent(native_string_to_bytes(yaml))
        config = _merge_configs_from_env(u"{}:{}".format(
            a.asTextMode().path,
            b.asTextMode().path,
        ))
        self.assertEqual(
            u"foo",
            config.current_context,
        )


    def test_collections_merged(self):
        """
        Non-conflicting items in collections (clusters, users, contexts) are
        merged.
        """
        foo_context = {
            u"name": u"foo",
            u"context": {u"cluster": u"foo", u"user": u"foo"},
        }
        bar_context = {
            u"name": u"foo",
            u"context": {u"cluster": u"foo", u"user": u"foo"},
        }
        foo_user = {
            u"name": u"foo",
            u"user": {u"x-magic": u"foo"},
        }
        bar_user = {
            u"name": u"bar",
            u"user": {u"x-magic": u"bar"},
        }
        foo_cluster = {
            u"name": u"foo",
            u"cluster": {u"server": u"foo"},
        }
        bar_cluster = {
            u"name": u"bar",
            u"cluster": {u"server": u"bar"},
        }
        a = self.configs.child(b"a")
        b = self.configs.child(b"b")
        yaml = safe_dump({
            u"apiVersion": u"v1",
            u"kind": u"Config",
            u"contexts": [foo_context],
            u"users": [foo_user],
            u"clusters": [foo_cluster],
        })
        a.setContent(native_string_to_bytes(yaml))
        yaml = safe_dump({
            u"apiVersion": u"v1",
            u"kind": u"Config",
            u"contexts": [bar_context],
            u"users": [bar_user],
            u"clusters": [bar_cluster],
        })
        b.setContent(native_string_to_bytes(yaml))
        config = _merge_configs_from_env(u"{}:{}".format(
            a.asTextMode().path,
            b.asTextMode().path,
        ))
        self.expectThat(
            config.doc[u"contexts"],
            Equals([foo_context, bar_context]),
        )
        self.expectThat(
            config.doc[u"users"],
            Equals([foo_user, bar_user]),
        )
        self.expectThat(
            config.doc[u"clusters"],
            Equals([foo_cluster, bar_cluster]),
        )



class KubernetesClientIntegrationTests(kubernetes_client_tests(get_kubernetes)):
    """
    Integration tests which interact with a network-accessible
    Kubernetes deployment via ``txkube.network_kubernetes``.
    """



class CollectionLocationTests(TestCase):
    """
    Tests for ``collection_location``.
    """
    def _test_collection_location(self, version, kind, expected, namespace, instance):
        """
        Verify that ``collection_location`` for a particular version, kind,
        namespace, and Python object.

        :param unicode version: The *apiVersion* of the object to test.
        :param unicode kind: The *kind* of the object to test.

        :param tuple[unicode] expected: A representation of the path of the
            URL which should be produced.

        :param namespace: The namespace the Python object is to claim - a
            ``unicode`` string or ``None``.

        :param bool instance: Whether to make the Python object an instance
            (``True``) or a class (``False``)..
        """
        k = kind
        n = namespace
        @implementer(IObject)
        class Mythical(object):
            apiVersion = version
            kind = k

            metadata = v1.ObjectMeta(namespace=n)

            def serialize(self):
                return {}

        verifyClass(IObject, Mythical)

        if instance:
            o = Mythical()
        else:
            o = Mythical

        self.assertThat(
            collection_location(o),
            Equals(expected),
        )


    def test_v1_type(self):
        """
        ``collection_location`` returns a tuple representing an URL path like
        */api/v1/<kind>s* when called with an ``IObject`` implementation of a
        *v1* Kubernetes object kind.
        """
        self._test_collection_location(
            u"v1", u"Mythical", (u"api", u"v1", u"mythicals"),
            namespace=None,
            instance=False,
        )


    def test_v1_instance(self):
        """
        ``collection_location`` returns a tuple representing an URL path like
        */api/v1/namespace/<namespace>/<kind>s* when called with an
        ``IObject`` provider representing Kubernetes object of a *v1* kind.
        """
        self._test_collection_location(
            u"v1", u"Mythical",
            (u"api", u"v1", u"namespaces", u"ns", u"mythicals"),
            namespace=u"ns",
            instance=True,
        )


    def test_v1beta1_type(self):
        """
        ``collection_location`` returns a tuple representing an URL path like
        */apis/extensions/v1beta1/<kind>s* when called with an ``IObject``
        implementation of a *v1beta1* Kubernetes object kind.
        """
        self._test_collection_location(
            u"v1beta1", u"Mythical",
            (u"apis", u"extensions", u"v1beta1", u"mythicals"),
            namespace=None,
            instance=False,
        )


    def test_v1beta1_instance(self):
        """
        ``collection_location`` returns a tuple representing an URL path like
        */apis/extensions/v1beta1/<kind>s* when called with an ``IObject``
        implementation of a *v1beta1* Kubernetes object kind.
        """
        self._test_collection_location(
            u"v1beta1", u"Mythical",
            (u"apis", u"extensions", u"v1beta1", u"namespaces", u"ns",
             u"mythicals"),
            namespace=u"ns",
            instance=True,
        )


class ExtraNetworkClientTests(TestCase):
    """
    Direct tests for ``_NetworkClient`` that go beyond the guarantees of
    ``IKubernetesClient``.
    """
    @capture_logging(
        lambda self, logger: self.expectThat(
            logger.messages,
            AnyMatch(ContainsDict({
                u"action_type": Equals(u"network-client:list"),
                u"apiVersion": Equals(u"v1"),
                u"kind": Equals(u"Pod"),
            })),
        ),
    )
    def test_list_logging(self, logger):
        """
        ``_NetworkClient.list`` logs an Eliot event describing its given type.
        """
        client = network_kubernetes(
            base_url=URL.fromText(u"http://127.0.0.1/"),
            agent=Agent(MemoryReactorClock()),
        ).client()
        client.list(v1.Pod)



def self_signed_certificate_paths(key_path, cert_path, name):
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=1024,
        backend=default_backend(),
    )
    key_path.setContent(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))

    certificate = cert(name, name, key.public_key(), key, True)
    cert_path.setContent(certificate.public_bytes(serialization.Encoding.PEM))

    return key_path, cert_path



class NetworkKubernetesFromContextTests(TwistedTestCase):
    """
    Direct tests for ``network_kubernetes_from_context``.
    """
    def test_client_chain_certificate(self):
        """
        A certificate chain in the *client-certificate* section of in the kube
        configuration file is used to configure the TLS context used when
        connecting to the API server.
        """
        def sign_ca_cert(key, requestObject, dn):
            from OpenSSL.crypto import X509, X509Extension
            from twisted.internet.ssl import Certificate

            req = requestObject.original
            cert = X509()
            dn._copyInto(cert.get_issuer())
            cert.set_subject(req.get_subject())
            cert.set_pubkey(req.get_pubkey())
            cert.gmtime_adj_notBefore(0)
            cert.gmtime_adj_notAfter(60 * 60)
            cert.set_serial_number(1)
            cert.add_extensions([
                X509Extension(b"basicConstraints", True, b"CA:TRUE"),
                # Not necessarily a good way to populate subjectAltName but it
                # quiets the deprecation warning we get from service_identity.
                X509Extension(b"subjectAltName", True, b"DNS:" + dn.commonName),
            ])
            cert.sign(key.original, "sha256")
            return Certificate(cert)

        ca_key = KeyPair.generate()
        ca_req = ca_key.requestObject(DN(commonName="kubernetes"))
        ca_cert = sign_ca_cert(ca_key, ca_req, DN(commonName="kubernetes"))

        intermediate_key = KeyPair.generate()
        intermediate_req = intermediate_key.requestObject(DN(commonName="intermediate"))
        intermediate_cert = sign_ca_cert(ca_key, intermediate_req, DN(commonName="kubernetes"))

        client_key = KeyPair.generate()
        client_req = client_key.requestObject(DN(commonName="client"))
        client_cert = intermediate_key.signRequestObject(DN(commonName="intermediate"), client_req, 1)

        chain = b"".join([
            client_cert.dumpPEM(),
            intermediate_cert.dumpPEM(),
        ])

        FilePath("ca.key").setContent(ca_key.dump(FILETYPE_PEM))
        FilePath("ca.crt").setContent(ca_cert.dump(FILETYPE_PEM))
        FilePath("intermediate.crt").setContent(intermediate_cert.dump(FILETYPE_PEM))
        FilePath("client.key").setContent(client_key.dump(FILETYPE_PEM))
        FilePath("client.crt").setContent(client_cert.dump(FILETYPE_PEM))
        FilePath("chain.crt").setContent(chain)

        config = self.write_config(ca_cert, chain, client_key)
        kubernetes = lambda reactor: network_kubernetes_from_context(
            reactor, "foo-ctx", path=config,
        )
        return self.check_tls_config(ca_key, ca_cert, kubernetes)


    def check_tls_config(self, ca_key, ca_cert, get_kubernetes):
        """
        Verify that a TLS server configured with the given key and certificate and
        the Kubernetes client returned by ``get_kubernetes`` can negotiate a
        TLS connection.
        """
        # Set up an HTTPS server that requires the certificate chain from the
        # configuration file.  This, because there's no way to pry inside a
        # Context and inspect its state nor any easy way to make Agent talk
        # over an in-memory transport.
        from twisted.internet import reactor
        endpoint = SSL4ServerEndpoint(
            reactor,
            0,
            CertificateOptions(
                privateKey=ca_key.original,
                certificate=ca_cert.original,
                trustRoot=trustRootFromCertificates([ca_cert]),
            ),
        )
        root = Resource()
        root.putChild(b"", Data(b"success", "text/plain"))

        # Construct the Kubernetes client objects with a Redirectable reactor.
        # This is necessary because the URL we pass to the Agent we get needs
        # to agree with the configuration file that was already written (or it
        # won't select the right client certificate).  Just one of the many
        # reasons it would be better if we didn't have to do real networking
        # here.
        redirectable = Redirectable(reactor)
        client = get_kubernetes(redirectable).client()
        agent = client.agent

        d = endpoint.listen(Site(root))
        def listening(port):
            self.addCleanup(port.stopListening)
            redirectable.set_redirect(port.getHost().host, port.getHost().port)
            url = b"https://127.0.0.1:8443/"
            return agent.request(b"GET", url)
        d.addCallback(listening)
        return d


    def write_config(self, ca_cert, chain, client_key):
        """
        Dump a kubectl config with the given details and return its location.

        :param Certificate ca_cert: The certificate authority certificate
            expected to have signed the server's certificate.

        :param unicode chain: PEM-encoded certificates starting with the
            client certificate and proceeding along a signature chain to a
            certificate signed by a certificate authority which the server
            recognizes.

        :param KeyPair client_key: The client's private key.

        :return FilePath: The path to the written configuration file.
        """
        config_file = FilePath(self.mktemp())
        config = safe_dump({
            "apiVersion": "v1",
            "kind": "Config",
            "contexts": [
                {
                    "name": "foo-ctx",
                    "context": {
                        "cluster": "foo-cluster",
                        "user": "foo-user",
                    },
                },
            ],
            "clusters": [
                {
                    "name": "foo-cluster",
                    "cluster": {
                        "certificate-authority-data": b64encode(ca_cert.dump(FILETYPE_PEM)),
                        "server": "https://127.0.0.1:8443/",
                    },
                },
            ],
            "users": [
                {
                    "name": "foo-user",
                    "user": {
                        "client-certificate-data": b64encode(chain),
                        "client-key-data": b64encode(client_key.dump(FILETYPE_PEM)),
                    },
                },
            ],
        })
        config_file.setContent(native_string_to_bytes(config))
        return config_file


    def test_kubeconfig_environment(self):
        """
        If ``KUBECONFIG`` is set in the environment,
        ``network_kubernetes_from_context`` merges the configuration from the
        files it refers to and returns an ``IKubernetes`` provider based on
        that configuration.
        """
        key_path, cert_path = self_signed_certificate_paths(
            FilePath(self.mktemp()),
            FilePath(self.mktemp()),
            u"x.invalid",
        )

        userauth = {
            "client-certificate": cert_path.path,
            "client-key": key_path.path,
        }

        a = FilePath(self.mktemp())
        yaml = safe_dump({
            "apiVersion": "v1",
            "kind": "Config",
            "contexts": [
                {"name": "a", "context": {"cluster": "a", "user": "a"}},
            ],
            "clusters": [{
                "name": "a",
                "cluster": {
                    "server": "https://a.example.com/",
                    "certificate-authority": cert_path.path,
                },
            }],
            "users": [
                {"name": "a", "user": userauth},
            ],
        })
        a.setContent(native_string_to_bytes(yaml))
        b = FilePath(self.mktemp())
        yaml = safe_dump({
            "apiVersion": "v1",
            "kind": "Config",
            "contexts": [
                {"name": "b", "context": {"cluster": "b", "user": "a"}},
            ],
            "clusters": [{
                "name": "b",
                "cluster": {
                    "server": "https://b.example.com/",
                    "certificate-authority": cert_path.path,
                },
            }],
            "users": [
                {"name": "b", "user": userauth},
            ],
        })
        b.setContent(native_string_to_bytes(yaml))
        c = FilePath(self.mktemp())
        yaml = safe_dump({
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "b",
        })
        c.setContent(native_string_to_bytes(yaml))
        kubeconfig = pathsep.join((a.path, b.path, c.path))
        kubernetes = network_kubernetes_from_context(
            MemoryReactorClock(),
            context=None,
            environ={"KUBECONFIG": kubeconfig},
        )
        self.assertEqual(
            "https://b.example.com/",
            kubernetes.base_url.asText(),
        )


    def test_default_os_environ(self):
        """
        If no value is passed for the ``environ`` parameter then
        ``network_kubernetes_from_context`` uses ``os.environ`` to look up the
        possible value of ``KUBECONFIG``.
        """
        key_path, cert_path = self_signed_certificate_paths(
            FilePath(self.mktemp()),
            FilePath(self.mktemp()),
            u"x.invalid",
        )
        userauth = {
            "client-certificate": cert_path.path,
            "client-key": key_path.path,
        }
        config = FilePath(self.mktemp())
        yaml = safe_dump({
            "apiVersion": "v1",
            "kind": "Config",
            "contexts": [
                {"name": "a", "context": {"cluster": "a", "user": "a"}},
            ],
            "clusters": [{
                "name": "a",
                "cluster": {
                    "server": "https://a.example.com/",
                    "certificate-authority": cert_path.path,
                },
            }],
            "users": [
                {"name": "a", "user": userauth},
            ],
        })
        config.setContent(native_string_to_bytes(yaml))
        import os
        self.patch(os, "environ", {u"KUBECONFIG": config.path})

        kubernetes = network_kubernetes_from_context(
            MemoryReactorClock(),
            context=u"a",
        )
        self.assertEqual(
            "https://a.example.com/",
            kubernetes.base_url.asText(),
        )


    def test_missing_kubeconfig(self):
        """
        When ``network_kubernetes_from_context`` is given no value for
        ``default_config_path`` it uses ``~/.kube/config`` as the value for
        that parameter.
        """
        callargs = getcallargs(network_kubernetes_from_context, MemoryReactorClock())
        self.assertEqual(
            FilePath(expanduser("~/.kube/config")),
            callargs["default_config_path"],
        )


    def test_default_config_path(self):
        """
        When ``network_kubernetes_from_context`` does not find ``KUBECONFIG`` in
        the environment it uses ``default_config_path`` as the path to the
        configuration file.
        """
        key_path, cert_path = self_signed_certificate_paths(
            FilePath(self.mktemp()),
            FilePath(self.mktemp()),
            u"x.invalid",
        )
        userauth = {
            "client-certificate": cert_path.path,
            "client-key": key_path.path,
        }
        config = FilePath(self.mktemp())
        yaml = safe_dump({
            "apiVersion": "v1",
            "kind": "Config",
            "contexts": [
                {"name": "a", "context": {"cluster": "a", "user": "a"}},
            ],
            "clusters": [{
                "name": "a",
                "cluster": {
                    "server": "https://a.example.com/",
                    "certificate-authority": cert_path.path,
                },
            }],
            "users": [
                {"name": "a", "user": userauth},
            ],
        })
        config.setContent(native_string_to_bytes(yaml))

        kubernetes = network_kubernetes_from_context(
            MemoryReactorClock(),
            context=u"a",
            environ={},
            default_config_path=config,
        )
        self.assertEqual(
            "https://a.example.com/",
            kubernetes.base_url.asText(),
        )



class MemoTests(TestCase):
    """
    Tests for ``_Memo``.
    """
    def test_get_empty(self):
        """
        In the empty state, ``get`` calls the given function and returns a
        ``Deferred`` that fires with that function's result.
        """
        result = object()
        def f():
            return succeed(result)

        m = _Memo()
        d = m.get(f)
        self.assertThat(d, succeeded(Equals(result)))


    def test_get_running(self):
        """
        In the running state, ``get`` does not call the given function and returns
        a ``Deferred`` that fires with the result of the function passed to
        the earlier ``get`` call.
        """
        result_obj = object()
        result = Deferred()
        def f1():
            return result
        f2 = None

        m = _Memo()
        d1 = m.get(f1)
        d2 = m.get(f2)
        result.callback(result_obj)
        self.assertThat(d1, succeeded(Equals(result_obj)))
        self.assertThat(d2, succeeded(Equals(result_obj)))


    def test_get_value(self):
        """
        In the value state, ``get`` does not call the given function and returns a
        ``Deferred`` that fires with the result of the function passed to the
        earlier ``get`` call.
        """
        result_obj = object()
        result = succeed(result_obj)
        def f1():
            return result
        f2 = None

        m = _Memo()
        d1 = m.get(f1)
        d2 = m.get(f2)
        self.assertThat(d1, succeeded(Equals(result_obj)))
        self.assertThat(d2, succeeded(Equals(result_obj)))



@attr.s
class Redirectable(proxyForInterface(IReactorSSL),
                   proxyForInterface(IReactorTCP),
                   proxyForInterface(IReactorTime),
                   proxyForInterface(IReactorPluggableNameResolver)):
    """
    A reactor which ignores the requested destination and always
    connects to an alternate address instead.

    :ivar host: The host portion of the alternate address.
    :ivar port: The host portion of the alternate address.
    """
    original = attr.ib()

    def set_redirect(self, host, port):
        """
        Specify the alternate address to which connections will be directed.
        """
        self.host, self.port = host, port


    def connectSSL(self, host, port, *a, **kw):
        """
        Establish a TLS connection to the alternate address instead of the given
        address.
        """
        return self.original.connectSSL(self.host, self.port, *a, **kw)


    def connectTCP(self, host, port, *a, **kw):
        """
        Establish a TCP connection to the alternate address instead of the given
        address.
        """
        return self.original.connectTCP(self.host, self.port, *a, **kw)
