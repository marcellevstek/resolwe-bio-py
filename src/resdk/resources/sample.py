"""Sample resource."""
import logging
from typing import TYPE_CHECKING

from resdk.shortcuts.sample import SampleUtilsMixin

from ..utils.decorators import assert_object_exists
from .background_task import BackgroundTask
from .collection import BaseCollection, Collection

if TYPE_CHECKING:
    from .annotations import AnnotationValue


class Sample(SampleUtilsMixin, BaseCollection):
    """Resolwe Sample resource.

    :param resolwe: Resolwe instance
    :type resolwe: Resolwe object
    :param model_data: Resource model data

    """

    endpoint = "sample"

    WRITABLE_FIELDS = BaseCollection.WRITABLE_FIELDS + ("collection",)

    def __init__(self, resolwe, **model_data):
        """Initialize attributes."""
        self.logger = logging.getLogger(__name__)

        #: ``Collection``s that contains the ``Sample`` (lazy loaded)
        self._collection = None
        #: list of ``Relation`` objects in ``Collection`` (lazy loaded)
        self._relations = None
        #: background ``Sample`` of the current ``Sample``
        self._background = None
        #: is this sample background to any other sample?
        self._is_background = None

        super().__init__(resolwe, **model_data)

    def update(self):
        """Clear cache and update resource fields from the server."""
        self._collection = None
        self._relations = None
        self._background = None
        self._is_background = None

        super().update()

    @property
    @assert_object_exists
    def data(self):
        """Get data."""
        if self._data is None:
            self._data = self.resolwe.data.filter(entity=self.id)

        return self._data

    @property
    def collection(self):
        """Get collection."""
        return self._collection

    @collection.setter
    def collection(self, payload):
        """Set collection."""
        self._resource_setter(payload, Collection, "_collection")

    @property
    @assert_object_exists
    def relations(self):
        """Get ``Relation`` objects for this sample."""
        if self._relations is None:
            self._relations = self.resolwe.relation.filter(entity=self.id)

        return self._relations

    @property
    def background(self):
        """Get background sample of the current one."""
        if self._background is None:
            background_relation = list(
                self.resolwe.relation.filter(
                    type="background",
                    entity=self.id,
                    label="case",
                )
            )

            if len(background_relation) > 1:
                raise LookupError(
                    "Multiple backgrounds defined for sample '{}'".format(self.name)
                )
            elif not background_relation:
                raise LookupError(
                    "No background is defined for sample '{}'".format(self.name)
                )

            for partition in background_relation[0].partitions:
                if (
                    partition["label"] == "background"
                    and partition["entity"] != self.id
                ):
                    self._background = self.resolwe.sample.get(id=partition["entity"])

            if self._background is None:
                # Cache the result = no background is found.
                self._background = False

        if self._background:
            return self._background

    @background.setter
    def background(self, bground):
        """Set sample background."""

        def count_cases(entity, label):
            """Get a tuple (relation, number_of_cases) in a specified relation.

            Relation is specified by collection, type-background'entity and label.
            """
            relation = list(
                self.resolwe.relation.filter(
                    collection=collection.id,
                    type="background",
                    entity=entity.id,
                    label=label,
                )
            )
            if len(relation) > 1:
                raise ValueError(
                    'Multiple relations of type "background" for sample {} in '
                    "collection {} "
                    "with label {}.".format(entity, collection, label)
                )
            elif len(relation) == 1:
                cases = len(
                    [
                        prt
                        for prt in relation[0].partitions
                        if prt.get("label") == "case"
                    ]
                )
            else:
                cases = 0

            return (relation[0] if relation else None, cases)

        if self.background == bground:
            return

        assert isinstance(bground, Sample)

        # Relations are always defined on collections: it is necessary
        # to check that both, background and case are defined in only
        # one common collection. Actions are done on this collection.
        if self.collection.id != bground.collection.id:
            raise ValueError(
                "Background and case sample are not in the same collection."
            )
        collection = self.collection

        # One cannot simply assign a background to sample but needs to
        # account also for already existing background relations they
        # are part of. By this, 3 x 3 scenarios are possible. One
        # dimension of scenarios is determined by the relation in which
        # *sample* is. It can be in no background relation (0), it can
        # be in background relation where only one sample is the case
        # sample (1) or it can be in background relation where many
        # case samples are involved (2). Similarly, (future, to-be)
        # background relation can be without any existing background
        # relation (0), in background relation with one (1) or more (2)
        # case samples.

        # Get background relation for this sample and count cases in it.
        # If no relation is found set to 0.
        self_relation, self_cases = count_cases(self, "case")

        # Get background relation of to-be background sample and count
        # cases in it. If no relation is found set to 0.
        bground_relation, bground_cases = count_cases(bground, "background")

        # 3 x 3 options reduce to 5, since some of them can be treated equally:
        if self_cases == bground_cases == 0:
            # Neither case nor background is in background relation.
            # Make a new background relation.
            collection.create_background_relation("Background", bground, [self])
        elif self_cases == 0 and bground_cases > 0:
            # Sample is not part of any existing background relation,
            # but background sample is. In this cae, just add sample to
            # alread existing background relation
            bground_relation.add_sample(self, label="case")
        elif self_cases == 1 and bground_cases == 0:
            # Sample si part od already existing background relation
            # where there is one sample and one background. New,
            # to-be-background sample is not part of any background
            # relation yet. Modify sample relation and replace background.
            for partition in self_relation.partitions:
                if partition["label"] == "background":
                    partition["entity"] = bground.id
                    break
        elif self_cases == 1 and bground_cases > 0:
            # Sample si part od already existing background relation
            # where there is one sample and one background. New,
            # to-be-background sample is is similar two-member relation.
            # Remove relaton of case sample and add it to existing
            # relation of the background smaple.
            self_relation.delete(force=True)
            bground_relation.add_sample(self, label="case")
        elif self_cases > 1:
            raise ValueError(
                "This sample is a case in a background relation with also other samples as cases. "
                "If you would like to change background sample for all of them please delete "
                "current relation and create new one with desired background."
            )

        self.save()
        self._relations = None
        self._background = None
        bground._is_background = True

    @property
    def is_background(self):
        """Return ``True`` if given sample is background to any other and ``False`` otherwise."""
        if self._is_background is None:
            background_relations = self.resolwe.relation.filter(
                type="background",
                entity=self.id,
                label="background",
            )
            # we need to iterate ``background_relations`` (using len) to
            # evaluate ResolweQuery:
            self._is_background = len(background_relations) > 0

        return self._is_background

    @assert_object_exists
    def duplicate(self):
        """Duplicate (make copy of) ``sample`` object.

        :return: Duplicated sample
        """
        task_data = self.api().duplicate.post({"ids": [self.id]})
        background_task = BackgroundTask(resolwe=self.resolwe, **task_data)
        return self.resolwe.sample.get(id__in=background_task.result())

    @property
    def annotations(self):
        """Get the annotations for the given sample."""
        return self.resolwe.annotation_value.filter(entity=self.id)

    def get_annotation(self, full_path: str) -> "AnnotationValue":
        """Get the AnnotationValue from full path.

        :raises LookupError: when field at the specified path does not exist.
        """
        group_name, field_name = full_path.split(".", maxsplit=1)
        return self.annotations.get(
            field__name=field_name, field__group__name=group_name
        )

    def set_annotation(self, full_path: str, value) -> "AnnotationValue":
        """Create/update annotation value."""
        try:
            annotation_value = self.get_annotation(full_path)
            annotation_value.value = value
            annotation_value.save()
        except LookupError:
            field = self.resolwe.annotation_field.from_path(full_path)
            annotation_value = self.resolwe.annotation_value.create(
                sample=self, field=field, value=value
            )
        return annotation_value
