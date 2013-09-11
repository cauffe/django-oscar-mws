import logging

from django.utils.translation import ugettext_lazy as _

from oscar_mws.fulfillment import FulfillmentOrderCreator

logger = logging.getLogger('oscar_mws')


def submit_order_to_mws(order, user, **kwargs):
    print "SUBMITTING ORDER"
    if kwargs.get('raw', False):
        return

    order_creator = FulfillmentOrderCreator()
    submitted_orders = order_creator.create_fulfillment_order(order)

    if not order_creator.errors:
        logger.info(
            _("Successfully submitted {0} orders to Amazon").format(
                len(submitted_orders)
            )
        )
    else:
        for order_id, error in order_creator.errors.iteritems():
            logger.error(
                _("Error submitting order {0} to Amazon: {1}").format(
                    order_id,
                    error
                )
            )