#   Copyright 2011 OpenPlans and contributors
#
#   This file is part of OpenBlock
#
#   OpenBlock is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   OpenBlock is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with OpenBlock.  If not, see <http://www.gnu.org/licenses/>.
#

"""
Forms for use in the openblock admin UI.
"""
# -*- coding: utf-8 -*-
from django import forms
from django.contrib import messages
from django.contrib.admin.helpers import Fieldset
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.csrf import csrf_protect
from ebdata.scrapers.general.spreadsheet import retrieval
from ebpub.db.bin import import_locations
from ebpub.db.models import LocationType
from ebpub.db.models import Schema, Lookup
from ebpub.metros.allmetros import get_metro
from re import findall
from tasks import CENSUS_STATES, download_state_shapefile, import_blocks_from_shapefiles
from tempfile import mkstemp, mkdtemp
import glob
import os
import zipfile


class SchemaLookupsForm(forms.Form):
    def __init__(self, lookup_ids, *args, **kwargs):
        super(SchemaLookupsForm, self).__init__(*args, **kwargs)
        for look_id in lookup_ids:
            self.fields['%s-name' % look_id] = forms.CharField(widget=forms.TextInput(attrs={'size': 50}))
            self.fields['%s-name' % look_id].lookup_obj = Lookup.objects.get(id=look_id)
            self.fields['%s-description' % look_id] = forms.CharField(required=False, widget=forms.Textarea())

class BlobSeedForm(forms.Form):
    rss_url = forms.CharField(max_length=512, widget=forms.TextInput(attrs={'size': 80}))
    site_url = forms.CharField(max_length=512, widget=forms.TextInput(attrs={'size': 80}))
    rss_full_entry = forms.BooleanField(required=False)
    pretty_name = forms.CharField(max_length=128, widget=forms.TextInput(attrs={'size': 80}))
    guess_article_text = forms.BooleanField(required=False)
    strip_noise = forms.BooleanField(required=False)

def save_file(f, suffix=''):
    import mimetypes
    if not suffix:
        # Try to come up with a reasonable suffix, since sometimes I
        # care what this thing is.  First try the declared type...
        mtype = None
        if hasattr(f, 'content_type'):
            mtype = f.content_type
            # ... although sometimes this returns nothing;
            # eg. mimetypes doesn't know about 'application/msexcel'
            suffix = mimetypes.guess_extension(mtype)
    if not suffix:
        mtype = None
        if hasattr(f, 'name'):
            mtype = mimetypes.guess_type(f.name)[0]
        if not mtype:
            # Fall back to guessing on the file's content.
            import magic
            guesser = magic.Magic(mime=True)
            f.file.seek(0)
            mtype = guesser.from_buffer(f.file.read(2048))
            f.file.seek(0)
        if mtype:
            if mtype == 'text/plain':
                # Special case since mimetypes.guess_extension()
                # returns something arbitrarily absurd for this, like .ksh
                suffix = '.txt'
            else:
                suffix = mimetypes.guess_extension(mtype)
        #print "GUESSED SUFFIX", suffix
    fd, name = mkstemp(suffix)
    fp = os.fdopen(fd, 'wb')
    for chunk in f.chunks():
        fp.write(chunk)
    fp.close()
    return name


class ImportZipcodeShapefilesForm(forms.Form):

    # Don't have one chosen by default.
    no_state = ''
    state = forms.TypedChoiceField(required=True,
                                   choices=((no_state, no_state),) + CENSUS_STATES,
                                   empty_value=no_state)
    zip_codes = forms.CharField(required=True, widget=forms.Textarea())

    def save(self):
        if not self.is_valid():
            return False

        zip_codes = findall('\d{5}', self.cleaned_data['zip_codes'])
        download_state_shapefile(self.cleaned_data['state'], zip_codes)

        return True


class UploadShapefileForm(forms.Form):

    zipped_shapefile = forms.FileField(
        required=True,
        help_text=('Note that self-extracting .exe files are not supported. If you '
                   'have one of those, extract it, then create a normal zip file '
                   'from its contents. Sorry for the inconvenience.'))

    def save(self):
        if not self.is_valid():
            return False
        self.shp_path = self.save_shapefile(self.cleaned_data['zipped_shapefile'])
        return True

    def save_shapefile(self, zipped_shapefile):
        # Unpack the zipped shapefile archive, gdal needs all
        # the files to be extracted and in the same directory.
        fd, zip_name = mkstemp('.zip')
        self.write_chunks(os.fdopen(fd, 'wb'),  zipped_shapefile)
        zfile = zipfile.ZipFile(zip_name)
        outdir = mkdtemp(suffix='-location-shapefiles')
        zfile.extractall(path=outdir)
        os.unlink(zip_name)
        # TODO: Some zipped shapefiles contain multiple .shp files!
        # We'll just assume you want the first one.
        shapefiles = glob.glob(os.path.join(outdir, '*shp'))
        if not shapefiles:
            for name in os.listdir(outdir):
                # Maybe there's a subdirectory?
                shapefiles = glob.glob(os.path.join(outdir, name, '*shp'))
                if shapefiles:
                    break
        assert shapefiles
        shapefile = shapefiles[0]
        return os.path.abspath(shapefile)

    def write_chunks(self, fp, f):
        try:
            for chunk in f.chunks():
                fp.write(chunk)
        finally:
            fp.close()


class PickShapefileLayerForm(forms.Form):
    shapefile = forms.CharField(required=True)

    # Would be nice to use a RelatedFieldWidgetWrapper here so we get
    # the "+" button to add new LocationTypes, but I haven't dug deep
    # enough to see how to rig that in to this context... AFAICT it
    # needs to wrap the widget instance, not just the widget class.
    location_type = forms.ModelChoiceField(queryset=LocationType.objects.all(),
                                           required=True,
                                           )
    layer = forms.IntegerField(required=True)
    name_field = forms.CharField(required=True)

    def save(self):
        if not self.is_valid():
              return False

        shapefile = os.path.abspath(self.cleaned_data['shapefile'])
        layer = import_locations.layer_from_shapefile(shapefile, self.cleaned_data['layer'])
        location_type = self.cleaned_data['location_type']
        name_field = self.cleaned_data['name_field']

        # TODO: Run this as a background task
        importer = import_locations.LocationImporter(layer, location_type,
                                                     filter_bounds=True)
        if importer.save(name_field) > 0:
            # TODO: validate this directory!
            import shutil
            shutil.rmtree(os.path.dirname(shapefile))
            return True
        else:
            # TODO: would be nice to pass some errors back to page
            return False


class ImportBlocksForm(forms.Form):
    edges = forms.FileField(label='All Lines (tl...edges.zip)',
                            required=True)
    featnames = forms.FileField(label='Feature Names Relationship (tl...featnames.zip)',
                                required=True)
    faces = forms.FileField(label='Topological Faces (tl...faces.zip)', required=True)
    place = forms.FileField(label='Place (Current) (tl..._place.zip)', required=True)

    city = forms.CharField(max_length=30, help_text="Optional: skip features that don't include this city name", required=False)

    fix_cities = forms.BooleanField(
        help_text="Optional: try to override each block's city by finding an overlapping Location that represents a city. Only useful if you've set up multiple_cities=True and set city_location_type in your settings.METRO_LIST *and* have some appropriate Locations of that type already created.",
        required=False, initial=bool(get_metro().get('multiple_cities', False)))

    regenerate_intersections = forms.BooleanField(
        help_text="Regenerate all Intersections and BlockIntersections after loading Blocks.  Say No only if you are sure you have more blocks to load from another set of shapefiles; it will run a lot faster. It's always safe to say Yes.",
        required=False, initial=True)

    def save(self):
        if not self.is_valid():
            return False

        import_blocks_from_shapefiles(
            save_file(self.cleaned_data['edges'], suffix='.zip'),
            save_file(self.cleaned_data['featnames'], suffix='.zip'),
            save_file(self.cleaned_data['faces'], suffix='.zip'),
            save_file(self.cleaned_data['place'], suffix='.zip'),
            city=self.cleaned_data['city'],
            fix_cities=self.cleaned_data['fix_cities'],
            regenerate_intersections=self.cleaned_data['regenerate_intersections'],
        )

        return True


def import_items_from_spreadsheets(items_file, schema, mapping_file=None,
                                   unique_fields=None):
    """
    Imports NewsItems from the given files; returns
    (number added, number changed, number skipped).
    """
    scraper = retrieval.SpreadsheetScraper(items_file,
                                           map_sheet_file=mapping_file,
                                           schema_slug=schema.slug,
                                           unique_fields=unique_fields,
                                           )
    scraper.update()
    return (scraper.num_added, scraper.num_changed, scraper.num_skipped)


class ImportNewsForm(forms.Form):

    items_file = forms.FileField(label='NewsItems spreadsheet',
                                 required=True)

    schema = forms.ModelChoiceField(queryset=Schema.objects.all(),
                                    required=True,
                                    )

    mapping_file = forms.FileField(label='Mapping spreadsheet',
                                   help_text=u'Describes which columns of the above spreadsheet are used for which fields of a NewsItem.  If not provided, the NewsItem spreadsheet must have column headers that match fields of NewsItem and/or attributes of the chosen Schema.',
                                   required=False)

    unique_fields = forms.MultipleChoiceField(
        label='Unique fields',
        help_text=u'Which NewsItem fields can be used to uniquely identify NewsItems of this schema.',
        choices = [(name, name) for name in retrieval.get_default_unique_field_names()],
        required=False,
        )


    added = updated = skipped = 0

    def save(self):
        if not self.is_valid():
            return False
        added, updated, skipped = import_items_from_spreadsheets(
            save_file(self.cleaned_data['items_file']),
            self.cleaned_data['schema'],
            mapping_file=self.cleaned_data.get('mapping_file') and save_file(self.cleaned_data['mapping_file']),
            unique_fields=self.cleaned_data['unique_fields'],
            )
        self.added = added
        self.updated = updated
        self.skipped = skipped
        return True
