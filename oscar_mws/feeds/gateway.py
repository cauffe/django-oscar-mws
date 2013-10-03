import logging

from lxml import etree
from cStringIO import StringIO
from dateutil.parser import parse as du_parse

from django.db.models import get_model

from .. import abstract_models as am
from ..connection import get_merchant_connection
from ..feeds import writers

logger = logging.getLogger('oscar_mws')

Product = get_model('catalogue', 'Product')
FeedReport = get_model("oscar_mws", "FeedReport")
FeedResult = get_model("oscar_mws", "FeedResult")
AmazonProfile = get_model('oscar_mws', 'AmazonProfile')
FeedSubmission = get_model("oscar_mws", "FeedSubmission")
MerchantAccount = get_model('oscar_mws', 'MerchantAccount')


class MwsFeedError(BaseException):
    pass


def handle_feed_submission_response(merchant, response, feed_xml=None):
    fsinfo = response.SubmitFeedResult.FeedSubmissionInfo

    try:
        submission = FeedSubmission.objects.get(
            submission_id=fsinfo.FeedSubmissionId,
            date_submitted=du_parse(fsinfo.SubmittedDate),
            feed_type=fsinfo.FeedType,
        )
    except FeedSubmission.DoesNotExist:
        submission = FeedSubmission(
            submission_id=fsinfo.FeedSubmissionId,
            date_submitted=du_parse(fsinfo.SubmittedDate),
            feed_type=fsinfo.FeedType,
        )
        if feed_xml:
            submission.feed_xml = feed_xml

    submission.merchant = merchant
    submission.processing_status = fsinfo.FeedProcessingStatus
    submission.save()

    logger.info(
        "Feed submission successful as ID {0}".format(submission.submission_id)
    )

    return submission


def submit_product_feed(products, marketplaces, dry_run=False):
    """
    Generate a product feed for the list of *products* and submit it to
    Amazon. The submission ID returned by them is used to create a
    FeedSubmission to log the progress of the submission as well as the
    products that are handled as part of the submission.

    A list of *marketplaces* is also required that specify the Amazon
    maketplaces to submit the product(s) to. The marketplaces have to be
    part of the same merchant account and have to have the same language code
    specified. If either of these restrictions is violated, the feed will be
    rejected by Amazon.
    """
    merchant_ids = set([m.merchant_id for m in marketplaces])
    marketplace_ids = [m.marketplace_id for m in marketplaces]
    if len(merchant_ids) > 1:
        raise MwsFeedError(
            "Marketplaces of different merchant accounts specified. This "
            "is invalid, please only use marketplaces for the same seller"
        )

    submissions = []
    for marketplace in marketplaces:
        merchant = marketplace.merchant
        logger.info(
            "Updating {0} products for seller ID {1}".format(
                len(products),
                merchant.seller_id,
            )
        )

        writer = writers.ProductFeedWriter(merchant_id=merchant.seller_id)
        for product in products:
            writer.add_product(product)
        xml_data = writer.as_string(pretty_print=dry_run)

        logger.debug("Product feed XML to be submitted:\n{0}".format(xml_data))

        if dry_run:
            print xml_data
            return

        mws_connection = get_merchant_connection(merchant.seller_id)
        response = mws_connection.submit_feed(
            FeedContent=xml_data,
            FeedType=am.TYPE_POST_PRODUCT_DATA,
            content_type='text/xml',
            MarketplaceIdList=marketplace_ids or merchant.marketplace_ids
        )
        submission = handle_feed_submission_response(merchant, response,
                                                     feed_xml=xml_data)
        for product in products:
            submission.submitted_products.add(product)
        submissions.append(submission)
    return submissions


def update_feed_submission(submission):
    connection = get_merchant_connection(submission.merchant.seller_id)
    response = connection.get_feed_submission_list(
        FeedSubmissionIdList=[submission.submission_id]
    )
    for result in response.GetFeedSubmissionListResult.FeedSubmissionInfo:
        submission.processing_status = result.FeedProcessingStatus
        submission.save()
    return submission


def update_feed_submissions(merchant):
    """
    Check the MWS API for updates on previously submitted feeds. If
    *submission_id* is specified only the feed submission matching that ID
    is requested. Otherwise, all submission that are stored in the database
    that are not _DONE_ or _CANCELLED_ are requested.

    Returns List of updated ``FeedSubmission`` instances.
    """
    submissions = FeedSubmission.objects.exclude(
        processing_status__in=[am.STATUS_DONE, am.STATUS_CANCELLED],
        merchant=merchant
    )

    api_kwargs = {}
    if submissions:
        api_kwargs['FeedSubmissionIdList'] = [
            s.submission_id for s in submissions
        ]

    connection = get_merchant_connection(merchant.seller_id)
    response = connection.get_feed_submission_list(**api_kwargs)

    if response.GetFeedSubmissionListResult.HasNext:
        #TODO: need to handle this flag
        response.GetFeedSubmissionListResult.NextToken

    updated_feeds = []
    for result in response.GetFeedSubmissionListResult.FeedSubmissionInfo:
        try:
            submission = FeedSubmission.objects.get(
                submission_id=result.FeedSubmissionId,
                date_submitted=du_parse(result.SubmittedDate),
                feed_type=result.FeedType,
            )
        except FeedSubmission.DoesNotExist:
            submission = FeedSubmission(
                submission_id=result.FeedSubmissionId,
                date_submitted=du_parse(result.SubmittedDate),
                feed_type=result.FeedType,
            )

        if submission.processing_status != result.FeedProcessingStatus:
            updated_feeds.append(submission)

        submission.merchant = merchant
        submission.processing_status = result.FeedProcessingStatus
        submission.save()

    return updated_feeds


def list_submitted_feeds(merchants=None):
    if not merchants:
        merchants = MerchantAccount.objects.all()

    feed_info = {}
    for merchant in merchants:
        mws_connection = get_merchant_connection(merchant.seller_id)
        response = mws_connection.get_feed_submission_list()

        feed_info[merchant.seller_id] = []
        for feed in response.GetFeedSubmissionListResult.FeedSubmissionInfo:
            feed_info[merchant.seller_id].append({
                'submission_id': feed.FeedSubmissionId,
                'feed_type': feed.FeedType,
                'status': feed.FeedProcessingStatus,
                'date_submitted': du_parse(
                    feed.get('SubmittedDate') or ''
                ),
                'date_processing_started': du_parse(
                    feed.get('StartedProcessingDate') or ''
                ),
                'date_processing_ended': du_parse(
                    feed.get('CompletedProcessingDate') or ''
                ),
            })

    return feed_info


def process_submission_results(submission):
    """
    Retrieve the submission results via the MWS API to check for errors in
    the submitted feed. The report and error data is stored in a submission
    report on the submission.

    If the submission was successful, we use the Inventory API to retrieve
    generated ASINs for new products and update the stock of the products.
    """
    logger.info(
        'Requesting submission result for {0}'.format(submission.submission_id)
    )
    connection = get_merchant_connection(submission.merchant.seller_id)
    response = connection.get_feed_submission_result(
        FeedSubmissionId=submission.submission_id
    )

    doc = etree.parse(StringIO(response))
    reports = []
    for report in doc.xpath('.//Message/ProcessingReport'):
        submission_id = int(report.find('DocumentTransactionID').text)

        if unicode(submission_id) != unicode(submission.submission_id):
            logger.warning(
                'received submission result for {0} when requesting '
                '{1}'.format(submission_id, submission.submission_id)
            )
            continue

        try:
            feed_report = FeedReport.objects.get(submission=submission)
        except FeedReport.DoesNotExist:
            feed_report = FeedReport(submission=submission)

        feed_report.status_code = report.find('StatusCode').text

        summary = report.find('ProcessingSummary')
        feed_report.processed = int(summary.find('MessagesProcessed').text)
        feed_report.successful = int(summary.find('MessagesSuccessful').text)
        feed_report.errors = int(summary.find('MessagesWithError').text)
        feed_report.warnings = int(summary.find('MessagesWithWarning').text)
        feed_report.save()

        reports.append(feed_report)

        for result in report.findall('Result'):
            feed_result = FeedResult(feed_report=feed_report)
            feed_result.message_code = result.find('ResultMessageCode').text
            feed_result.description = result.find('ResultDescription').text
            feed_result.type = result.find('ResultCode').text

            if result.find('AdditionalInfo/SKU') is not None:
                product_sku = result.find('AdditionalInfo/SKU').text
                try:
                    product = Product.objects.get(
                        product__stockrecord__partner_sku=product_sku
                    )
                except Product.DoesNotExist:
                    pass
                else:
                    feed_result.product = product
            feed_result.save()

    return reports


def update_product_identifiers(merchant, products):
    connection = get_merchant_connection(merchant.seller_id)
    for product in products:
        marketplace_ids = [
            m.marketplace_id for m in product.amazon_profile.marketplaces.all()
        ]
        if not marketplace_ids:
            marketplace_ids = [None]

        for marketplace_id in marketplace_ids:
            response = connection.get_matching_product_for_id(
                MarketplaceId=marketplace_id,
                IdType="SellerSKU",
                IdList=[product.amazon_profile.sku],
            )

            result = response.GetMatchingProductForIdResult
            if not result.get('status') == 'Success':
                logger.info(
                    'Skipping product with SKU {0}, no info available'.format(
                        response.get("Id")
                    )
                )
                continue

            for fprod in response.GetMatchingProductForIdResult.Products.Product:
                mp_asin = fprod.Identifiers.MarketplaceASIN
                marketplace_id = mp_asin.MarketplaceId
                asin = mp_asin.ASIN

                if asin:
                    AmazonProfile.objects.filter(**{
                        AmazonProfile.SELLER_SKU_FIELD: result.get("Id")
                    }).update(asin=asin)


def switch_product_fulfillment(marketplace, products, dry_run=False):
    writer = writers.InventoryFeedWriter(marketplace.merchant.seller_id)
    for product in products:
        writer.add_product(
            product,
            fulfillment_by=product.amazon_profile.fulfillment_by,
            fulfillment_center_id=marketplace.fulfillment_center_id,
        )

    xml_data = writer.as_string(pretty_print=dry_run)
    logger.debug(
        "Submitting inventory feed with XML:\n{0}".format(xml_data)
    )
    if dry_run:
        print xml_data
        return

    mws_connection = get_merchant_connection(marketplace.merchant.seller_id)
    response = mws_connection.submit_feed(
        FeedContent=xml_data,
        FeedType=am.TYPE_POST_INVENTORY_AVAILABILITY_DATA,
        content_type='text/xml',
    )
    return handle_feed_submission_response(marketplace.merchant, response,
                                           feed_xml=xml_data)
