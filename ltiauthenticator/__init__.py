import time

from traitlets import Dict
from tornado import gen, web

from jupyterhub.auth import Authenticator
from jupyterhub.handlers import BaseHandler
from jupyterhub.utils import url_path_join

from oauthlib.oauth1.rfc5849 import signature
from collections import OrderedDict

__version__ = '0.4.1.dev'

class LTILaunchValidator:
    # Record time when process starts, so we can reject requests made
    # before this
    PROCESS_START_TIME = int(time.time())

    # Keep a class-wide, global list of nonces so we can detect & reject
    # replay attacks. This possibly makes this non-threadsafe, however.
    nonces = OrderedDict()

    def __init__(self, consumers):
        self.consumers = consumers

    def validate_launch_request(
            self,
            launch_url,
            headers,
            args
    ):
        """
        Validate a given launch request

        launch_url: Full URL that the launch request was POSTed to
        headers: k/v pair of HTTP headers coming in with the POST
        args: dictionary of body arguments passed to the launch_url
            Must have the following keys to be valid:
                oauth_consumer_key, oauth_timestamp, oauth_nonce,
                oauth_signature
        """

        # Validate args!
        if 'oauth_consumer_key' not in args:
            raise web.HTTPError(401, "oauth_consumer_key missing")
        if args['oauth_consumer_key'] not in self.consumers:
            raise web.HTTPError(401, "oauth_consumer_key not known")

        if 'oauth_signature' not in args:
            raise web.HTTPError(401, "oauth_signature missing")
        if 'oauth_timestamp' not in args:
            raise web.HTTPError(401, 'oauth_timestamp missing')

        # Allow 30s clock skew between LTI Consumer and Provider
        # Also don't accept timestamps from before our process started, since that could be
        # a replay attack - we won't have nonce lists from back then. This would allow users
        # who can control / know when our process restarts to trivially do replay attacks.
        oauth_timestamp = int(float(args['oauth_timestamp']))
        if (
                int(time.time()) - oauth_timestamp > 30
                or oauth_timestamp < LTILaunchValidator.PROCESS_START_TIME
        ):
            raise web.HTTPError(401, "oauth_timestamp too old")

        if 'oauth_nonce' not in args:
            raise web.HTTPError(401, 'oauth_nonce missing')
        if (
                oauth_timestamp in LTILaunchValidator.nonces
                and args['oauth_nonce'] in LTILaunchValidator.nonces[oauth_timestamp]
        ):
            raise web.HTTPError(401, "oauth_nonce + oauth_timestamp already used")
        LTILaunchValidator.nonces.setdefault(oauth_timestamp, set()).add(args['oauth_nonce'])


        args_list = []
        for key, values in args.items():
            if type(values) is list:
                args_list += [(key, value) for value in values]
            else:
                args_list.append((key, values))

        base_string = signature.signature_base_string(
            'POST',
            signature.base_string_uri(launch_url),
            signature.normalize_parameters(
                signature.collect_parameters(body=args_list, headers=headers)
            )
        )

        consumer_secret = self.consumers[args['oauth_consumer_key']]

        sign = signature.sign_hmac_sha1(base_string, consumer_secret, None)
        is_valid = signature.safe_string_equals(sign, args['oauth_signature'])

        if not is_valid:
            raise web.HTTPError(401, "Invalid oauth_signature")

        return True


class LTIAuthenticator(Authenticator):
    """
    JupyterHub Authenticator for use with LTI based services (EdX, Canvas, etc)
    """

    auto_login = True
    login_service = 'LTI'

    consumers = Dict(
        {},
        config=True,
        help="""
        A dict of consumer keys mapped to consumer secrets for those keys.

        Allows multiple consumers to securely send users to this JupyterHub
        instance.
        """
    )

    def get_handlers(self, app):
        return [
            ('/lti/launch', LTIAuthenticateHandler)
        ]


    @gen.coroutine
    def authenticate(self, handler, data=None):
        # FIXME: Run a process that cleans up old nonces every other minute
        validator = LTILaunchValidator(self.consumers)

        args = {}
        for k, values in handler.request.body_arguments.items():
            args[k] = values[0].decode() if len(values) == 1 else [v.decode() for v in values]

        # handle multiple layers of proxied protocol (comma separated) and take the outermost
        if 'x-forwarded-proto' in handler.request.headers:
            # x-forwarded-proto might contain comma delimited values
            # left-most value is the one sent by original client
            hops = [h.strip() for h in handler.request.headers['x-forwarded-proto'].split(',')]
            protocol = hops[0]
        else:
            protocol = handler.request.protocol

        launch_url = protocol + "://" + handler.request.host + handler.request.uri

        if validator.validate_launch_request(
                launch_url,
                handler.request.headers,
                args
        ):

            # Use first part of email address as the login name:
            user_name = handler.get_body_argument('lis_person_contact_email_primary')
            user_name = user_name.split("@")[0] # delete '@...'

            # Alternatively, use a combination of first and last name as login name:
            # user_name = handler.get_body_argument('lis_person_name_family') + "_" + handler.get_body_argument('lis_person_name_given')

            # Attention: The above used parameter names may be deprecated in newer LTI versions. See 'https://www.imsglobal.org/specs/ltiv2p0/implementation-guide' (Appendix D – Deprecated Parameter Names).

            return {
                'name': user_name,
                'auth_state': {k: v for k, v in args.items() if not k.startswith('oauth_')}
            }

    def login_url(self, base_url):
        return url_path_join(base_url, '/lti/launch')


class LTIAuthenticateHandler(BaseHandler):
    """
    Handler for /lti/launch

    Implements v1 of the LTI protocol for passing authentication information
    through.

    If there's a custom parameter called 'next', will redirect user to
    that URL after authentication. Else, will send them to /home.
    """

    @gen.coroutine
    def post(self):
        """
        Technical reference of relevance to understand this function
        ------------------------------------------------------------
        1. Class dependencies
           - jupyterhub.handlers.BaseHandler: https://github.com/jupyterhub/jupyterhub/blob/abb93ad799865a4b27f677e126ab917241e1af72/jupyterhub/handlers/base.py#L69
           - tornado.web.RequestHandler: https://www.tornadoweb.org/en/stable/web.html#tornado.web.RequestHandler
        2. Function dependencies
           - login_user: https://github.com/jupyterhub/jupyterhub/blob/abb93ad799865a4b27f677e126ab917241e1af72/jupyterhub/handlers/base.py#L696-L715
             login_user is defined in the JupyterHub wide BaseHandler class,
             mainly wraps a call to the authenticate function and follow up.
             a successful authentication with a call to auth_to_user that
             persists a JupyterHub user and returns it.
           - get_next_url: https://github.com/jupyterhub/jupyterhub/blob/abb93ad799865a4b27f677e126ab917241e1af72/jupyterhub/handlers/base.py#L587
           - get_body_argument: https://www.tornadoweb.org/en/stable/web.html#tornado.web.RequestHandler.get_body_argument
        """
        # FIXME: Figure out if we want to pass the user returned from
        #        self.login_user() to self.get_next_url(). It is named
        #        _ for now as pyflakes is fine about having an unused
        #        variable named _.
        _ = yield self.login_user()
        next_url = self.get_next_url()
        body_argument = self.get_body_argument(
            name='custom_next',
            default=next_url,
        )

        self.redirect(body_argument)
