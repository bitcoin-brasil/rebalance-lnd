import sys
import math

from routes import Routes

DEFAULT_BASE_FEE_SAT_MSAT = 1000
DEFAULT_FEE_RATE_MSAT = 0.001
MAX_FEE_RATE = 1000


def debug(message):
    sys.stderr.write(message + "\n")


def debugnobreak(message):
    sys.stderr.write(message)


class Logic:
    def __init__(
            self,
            lnd,
            first_hop_channel,
            last_hop_channel,
            amount,
            channel_ratio,
            excluded,
            max_fee_factor,
            econ_fee,
            econ_fee_factor
    ):
        self.lnd = lnd
        self.first_hop_channel = first_hop_channel
        self.last_hop_channel = last_hop_channel
        self.amount = amount
        self.channel_ratio = channel_ratio
        self.excluded = []
        if excluded:
            self.excluded = excluded
        self.max_fee_factor = max_fee_factor
        self.econ_fee = econ_fee
        self.econ_fee_factor = econ_fee_factor
        if not self.econ_fee_factor:
            self.econ_fee_factor = 1.0

    def rebalance(self):
        fee_limit_msat = self.get_fee_limit_msat()
        if self.last_hop_channel:
            debug(("Sending {:,} satoshis to rebalance to channel with ID %d (%s)"
                   % (self.last_hop_channel.chan_id, self.lnd.get_node_alias(self.last_hop_channel.remote_pubkey)))
                  .format(self.amount))
        else:
            debug("Sending {:,} satoshis.".format(self.amount))
        if self.channel_ratio != 0.5:
            debug("Channel ratio used is %d%%" % int(self.channel_ratio * 100))
        if self.first_hop_channel:
            debug("Forced first channel has ID %d (%s)"
                  % (self.first_hop_channel.chan_id, self.lnd.get_node_alias(self.first_hop_channel.remote_pubkey)))

        payment_request = self.generate_invoice()
        min_fee_last_hop = None
        if self.econ_fee and self.first_hop_channel:
            policy_first_hop = self.lnd.get_policy_to(self.first_hop_channel.chan_id)
            fee_rate = policy_first_hop.fee_rate_milli_msat
            min_fee_last_hop = self.econ_fee_factor * self.compute_fee(self.amount, fee_rate, policy_first_hop)
        routes = Routes(
            self.lnd, payment_request, self.first_hop_channel, self.last_hop_channel, fee_limit_msat, min_fee_last_hop
        )

        self.initialize_ignored_channels(routes)

        tried_routes = []
        while routes.has_next():
            route = routes.get_next()

            success = self.try_route(payment_request, route, routes, tried_routes)
            if success:
                return True
        debug("Could not find any suitable route")
        return False

    def get_fee_limit_msat(self):
        fee_limit_msat = None
        if self.last_hop_channel and self.econ_fee:
            policy = self.lnd.get_policy_to(self.last_hop_channel.chan_id)
            fee_rate = policy.fee_rate_milli_msat
            if fee_rate > MAX_FEE_RATE:
                debug("Calculating using capped fee rate %s for inbound channel (original fee rate %s)"
                      % (MAX_FEE_RATE, fee_rate))
                fee_rate = MAX_FEE_RATE
            fee_limit_msat = self.econ_fee_factor * self.compute_fee(self.amount, fee_rate, policy)
            debug("Setting fee limit to %s (due to --econ-fee, factor %s)"
                  % (int(fee_limit_msat), self.econ_fee_factor))

        return fee_limit_msat

    def try_route(self, payment_request, route, routes, tried_routes):
        if self.route_is_invalid(route, routes):
            return False

        tried_routes.append(route)
        debug("")
        debug("Trying route #%d" % len(tried_routes))
        debug(Routes.print_route(route))

        response = self.lnd.send_payment(payment_request, route)
        is_successful = response.failure.code == 0
        if is_successful:
            last_hop_alias = self.lnd.get_node_alias(route.hops[-2].pub_key)
            first_hop_alias = self.lnd.get_node_alias(route.hops[0].pub_key)
            debug("")
            debug("")
            debug("")
            debug("Decreased inbound liquidity on %s by %d sats" % (last_hop_alias, route.hops[-1].amt_to_forward))
            debug("Increased inbound liquidity on %s" % first_hop_alias)
            debug("Fee: %d sats" % route.total_fees)
            debug("")
            debug("Successful route:")
            debug(Routes.print_route(route))
            return True
        else:
            self.handle_error(response, route, routes)
            return False

    @staticmethod
    def handle_error(response, route, routes):
        code = response.failure.code
        failure_source_pubkey = Logic.get_failure_source_pubkey(response, route)
        if code == 15:
            debugnobreak("Temporary channel failure, ")
            routes.ignore_edge_on_route(failure_source_pubkey, route)
        elif code == 18:
            debugnobreak("Unknown next peer, ")
            routes.ignore_edge_on_route(failure_source_pubkey, route)
        elif code == 12:
            debugnobreak("Fee insufficient, ")
            routes.ignore_edge_on_route(failure_source_pubkey, route)
        elif code == 14:
            debugnobreak("Channel disabled, ")
            routes.ignore_edge_on_route(failure_source_pubkey, route)
        else:
            debug(repr(response))
            debug("Unknown error code %s" % repr(code))

    @staticmethod
    def get_failure_source_pubkey(response, route):
        if response.failure.failure_source_index == 0:
            failure_source_pubkey = route.hops[-1].pub_key
        else:
            failure_source_pubkey = route.hops[response.failure.failure_source_index - 1].pub_key
        return failure_source_pubkey

    def route_is_invalid(self, route, routes):
        first_hop = route.hops[0]
        last_hop = route.hops[-1]
        if self.low_local_ratio_after_sending(first_hop, route.total_amt):
            debugnobreak("Outbound channel would have low local ratio after sending, ")
            routes.ignore_first_hop(self.get_channel_for_channel_id(first_hop.chan_id))
            return True
        if self.first_hop_and_last_hop_use_same_channel(first_hop, last_hop):
            debugnobreak("Outbound and inbound channel are identical, ")
            hop_before_last_hop = route.hops[-2]
            routes.ignore_edge_from_to(last_hop.chan_id, hop_before_last_hop.pub_key, last_hop.pub_key)
            return True
        if self.high_local_ratio_after_receiving(last_hop):
            debugnobreak("Inbound channel would have high local ratio after receiving, ")
            hop_before_last_hop = route.hops[-2]
            routes.ignore_edge_from_to(last_hop.chan_id, hop_before_last_hop.pub_key, last_hop.pub_key)
            return True
        if self.fees_too_high(route):
            routes.ignore_high_fee_hops(route)
            return True
        return False

    def low_local_ratio_after_sending(self, first_hop, total_amount):
        if self.first_hop_channel:
            # Just use the computed/specified amount to drain the first hop, ignoring fees
            return False
        channel_id = first_hop.chan_id
        channel = self.get_channel_for_channel_id(channel_id)
        if channel is None:
            debug("Unable to get channel information for hop %s" % repr(first_hop))
            return True

        remote = channel.remote_balance + total_amount
        local = channel.local_balance - total_amount
        ratio = float(local) / (remote + local)
        return ratio < self.channel_ratio

    def high_local_ratio_after_receiving(self, last_hop):
        if self.last_hop_channel:
            return False
        channel_id = last_hop.chan_id
        channel = self.get_channel_for_channel_id(channel_id)
        if channel is None:
            debug("Unable to get channel information for hop %s" % repr(last_hop))
            return True

        amount = last_hop.amt_to_forward
        remote = channel.remote_balance - amount
        local = channel.local_balance + amount
        ratio = float(local) / (remote + local)
        return ratio > self.channel_ratio

    @staticmethod
    def first_hop_and_last_hop_use_same_channel(first_hop, last_hop):
        return first_hop.chan_id == last_hop.chan_id

    def fees_too_high(self, route):
        if self.econ_fee:
            return self.fees_too_high_econ_fee(route)
        hops_with_fees = len(route.hops) - 1
        lnd_fees = hops_with_fees * (DEFAULT_BASE_FEE_SAT_MSAT + (self.amount * DEFAULT_FEE_RATE_MSAT))
        limit = self.max_fee_factor * lnd_fees
        high_fees = route.total_fees_msat > limit
        if high_fees:
            debugnobreak("High fees (%s sat over limit of %s), "
                         % (int((route.total_fees_msat - limit) / 1000), int(limit/1000)))
        return high_fees

    def fees_too_high_econ_fee(self, route):
        policy_first_hop = self.lnd.get_policy_to(route.hops[0].chan_id)
        amount = route.total_amt
        missed_fee = self.compute_fee(amount, policy_first_hop.fee_rate_milli_msat, policy_first_hop)
        policy_last_hop = self.lnd.get_policy_to(route.hops[-1].chan_id)
        fee_rate_last_hop = policy_last_hop.fee_rate_milli_msat
        original_fee_rate_last_hop = fee_rate_last_hop
        if fee_rate_last_hop > MAX_FEE_RATE:
            fee_rate_last_hop = MAX_FEE_RATE
        expected_fee = self.econ_fee_factor * self.compute_fee(amount, fee_rate_last_hop, policy_last_hop)
        rebalance_fee = route.total_fees
        high_fees = rebalance_fee + missed_fee > expected_fee
        if high_fees:
            difference = rebalance_fee + missed_fee - expected_fee
            if fee_rate_last_hop != original_fee_rate_last_hop:
                debug("Calculating using capped fee rate %s for inbound channel (original fee rate %s)"
                      % (MAX_FEE_RATE, original_fee_rate_last_hop))
            debugnobreak("High fees ("
                         "%s expected future fee income for inbound channel (factor %s), "
                         "have to pay %s now, "
                         "missing out on %s future fees for outbound channel, "
                         "difference %s), " % (
                             math.floor(expected_fee),
                             self.econ_fee_factor,
                             int(rebalance_fee),
                             math.ceil(missed_fee),
                             math.ceil(difference)
                         ))
        return high_fees

    @staticmethod
    def compute_fee(amount, fee_rate, policy):
        expected_fee_msat = amount / 1000000 * fee_rate + policy.fee_base_msat / 1000
        return expected_fee_msat

    def generate_invoice(self):
        if self.last_hop_channel:
            memo = "Rebalance of channel with ID %d" % self.last_hop_channel.chan_id
        else:
            memo = "Rebalance of channel with ID %d" % self.first_hop_channel.chan_id
        return self.lnd.generate_invoice(memo, self.amount)

    def get_channel_for_channel_id(self, channel_id):
        for channel in self.lnd.get_channels():
            if channel.chan_id == channel_id:
                if not hasattr(channel, 'local_balance'):
                    channel.local_balance = 0
                if not hasattr(channel, 'remote_balance'):
                    channel.remote_balance = 0
                return channel
        debug("Unable to find channel with id %d!" % channel_id)

    def initialize_ignored_channels(self, routes):
        if self.first_hop_channel:
            chan_id = self.first_hop_channel.chan_id
            from_pub_key = self.first_hop_channel.remote_pubkey
            to_pub_key = self.lnd.get_own_pubkey()
            routes.ignore_edge_from_to(chan_id, from_pub_key, to_pub_key, show_message=False)
            if not self.last_hop_channel:
                self.ignore_last_hops_with_high_ratio(routes)
        if self.last_hop_channel:
            chan_id = self.last_hop_channel.chan_id
            from_pub_key = self.lnd.get_own_pubkey()
            to_pub_key = self.last_hop_channel.remote_pubkey
            routes.ignore_edge_from_to(chan_id, from_pub_key, to_pub_key, show_message=False)
            if self.econ_fee:
                self.ignore_first_hops_with_fee_rate_higher_than_last_hop(routes)
        for channel in self.lnd.get_channels():
            if self.low_local_ratio_after_sending(channel, self.amount):
                routes.ignore_first_hop(channel, show_message=False)
            if channel.chan_id in self.excluded:
                debugnobreak("Channel is excluded, ")
                routes.ignore_first_hop(channel)

    def ignore_first_hops_with_fee_rate_higher_than_last_hop(self, routes):
        policy_last_hop = self.lnd.get_policy_to(self.last_hop_channel.chan_id)
        last_hop_fee_rate = policy_last_hop.fee_rate_milli_msat
        from_pub_key = self.lnd.get_own_pubkey()
        for channel in self.lnd.get_channels():
            chan_id = channel.chan_id
            policy = self.lnd.get_policy_to(chan_id)
            if policy.fee_rate_milli_msat > last_hop_fee_rate:
                to_pub_key = channel.remote_pubkey
                routes.ignore_edge_from_to(chan_id, from_pub_key, to_pub_key, show_message=False)

    def ignore_last_hops_with_high_ratio(self, routes):
        for channel in self.lnd.get_channels():
            channel_id = channel.chan_id
            if channel is None:
                return
            remote = channel.remote_balance - self.amount
            local = channel.local_balance + self.amount
            ratio = float(local) / (remote + local)
            if ratio > self.channel_ratio:
                to_pub_key = self.lnd.get_own_pubkey()
                routes.ignore_edge_from_to(channel_id, channel.remote_pubkey, to_pub_key, show_message=False)
