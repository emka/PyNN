"""
Enables creating neuronal network models in PyNN from a 9ML description.

Classes:
    Network -- container for a network model.

:copyright: Copyright 2006-2013 by the PyNN team, see AUTHORS.
:license: CeCILL, see LICENSE for details.
"""

import nineml.user_layer as nineml
import nineml.abstraction_layer as al
import pyNN.nineml
import pyNN.random
import pyNN.space
import re


def reverse_map(D):
    """
    Return a dict having D.values() as its keys and D.keys() as its values.
    """
    E = {}
    for k,v in D.items():
        if v in E:
            raise KeyError("Cannot reverse this mapping, as it is not one-to-one ('%s' would map to both '%s' and '%s')" % (v, E[v], k))
        E[v] = k
    return E


def scale(quantity):
    """Primitive unit handling. Should probably use Piquant, quantities, or similar."""
    factors = {
        'ms': 1,
        'mV': 1,
        's': 1000,
        'V': 1000,
        'Hz': 1,
        'nF': 1,
        'nA': 1,
        'unknown': 1,
        'dimensionless': 1,
    }
    return quantity.value * factors[quantity.unit]


def parameter_dimension(name, parameters):
    # parameters is a list of AL Parameter objects
    for p in parameters:
        if p.name == name:
            return p.dimension
    errmsg = "The parameter list does not contain a parameter called '%s'. Valid names are %s"
    raise KeyError(errmsg % (name, ", ".join([p.name for p in parameters])))


def resolve_parameters(nineml_component, random_distributions, resolve="parameters"):
    """
    Turn a 9ML ParameterSet or InitialValueSet into a Python dict, including turning 9ML
    RandomDistribution objects into PyNN RandomDistribution objects.
    """
    P = {}
    for name, p in getattr(nineml_component, resolve).items():
        qname = "%s_%s" % (nineml_component.name, name)
        if isinstance(p.value, nineml.RandomDistribution):
            rd = p.value
            if rd.name in random_distributions:
                P[qname] = random_distributions[rd.name]
            else:
                rd_name = reverse_map(pyNN.nineml.utility.random_distribution_url_map)[rd.definition.url]
                rd_param_names = pyNN.nineml.utility.random_distribution_parameter_map[rd_name]
                rd_params = [rd.parameters[rdp_name].value for rdp_name in rd_param_names]
                rand_distr = pyNN.random.RandomDistribution(rd_name, rd_params)
                P[qname] = rand_distr
                random_distributions[rd.name] = rand_distr
            P[qname] = -999
        elif p.value in ('True', 'False'):
            P[qname] = eval(p.value)
        elif isinstance(p.value, basestring):
            P[qname] = p.value
        else:
            P[qname] = scale(p)
    return P


def _build_structure(nineml_structure):
    """
    Return a PyNN Structure object that corresponds to the provided 9ML
    Structure object.

    For now, we do this by mapping names rather than parsing the 9ML abstraction
    layer file.
    """
    if nineml_structure:
        # ideally should parse abstraction layer file
        # for now we'll just match file names
        P = nineml_structure.parameters
        if "Grid2D" in nineml_structure.definition.url:
            pyNN_structure = pyNN.space.Grid2D(
                                aspect_ratio=P["aspect_ratio"].value,
                                dx=P["dx"].value,
                                dy=P["dy"].value,
                                x0=P["x0"].value,
                                y0=P["y0"].value,
                                fill_order=P["fill_order"].value)
        elif "Grid3D" in nineml_structure.definition.url:
            pyNN_structure = pyNN.space.Grid3D(
                                aspect_ratioXY=P["aspect_ratioXY"].value,
                                aspect_ratioXZ=P["aspect_ratioXZ"].value,
                                dx=P["dx"].value,
                                dy=P["dy"].value,
                                dz=P["dz"].value,
                                x0=P["x0"].value,
                                y0=P["y0"].value,
                                z0=P["z0"].value,
                                fill_order=P["fill_order"].value)
        elif "Line" in nineml_structure.definition.url:
            pyNN_structure = pyNN.space.Line(
                                dx=P["dx"].value,
                                x0=P["x0"].value,
                                y0=P["y0"].value,
                                z0=P["z0"].value)
        else:
            raise Exception("nineml_structure %s not supported by PyNN" % nineml_structure)
    else:
        pyNN_structure = None
    return pyNN_structure


def _generate_variable_name(name):
    return name.replace(" ", "_").replace("-", "")


class Network(object):
    """
    Container for a neuronal network model, created from a 9ML user-layer file.

    There is not a one-to-one mapping between 9ML and PyNN concepts. The two
    main differences are:
        (1) a 9ML Group contains both neurons (populations) and connections
            (projections), whereas a PyNN Assembly contains only neurons: the
            connections are contained in global Projections.
        (2) in 9ML, the post-synaptic response is defined in the projection,
            whereas in PyNN it is a property of the target population.

    Attributes:
        assemblies  -- a dict containing PyNN Assembly objects
        projections -- a dict containing PyNN Projection objects
    """

    def __init__(self, sim, nineml_model):
        """
        Instantiate a network from a 9ML file, in the specified simulator.
        """
        global random_distributions
        self.sim = sim
        if isinstance(nineml_model, basestring):
            self.nineml_model = nineml.parse(nineml_model)
        elif isinstance(nineml_model, nineml.Model):
            self.nineml_model = nineml_model
        else:
            raise TypeError("nineml_model must be a nineml.Model instance or the path to a NineML XML file.")
        self.random_distributions = {}
        self.assemblies = {}
        self.projections = {}
        _tmp = __import__(sim.__name__, globals(), locals(), ["nineml"])
        self._nineml_module = _tmp.nineml
        self._build()

    def _build(self):
        for group in self.nineml_model.groups.values():
            self._handle_group(group)

    def _handle_group(self, group):
        # create an Assembly
        self.assemblies[group.name] = self.sim.Assembly(label=group.name)

        # extract post-synaptic response definitions from projections
        self.psr_map = {}
        for projection in group.projections.values():
            if isinstance(projection.target, nineml.Selection):
                projection.target.evaluate(group)
                target_populations = [x[0] for x in projection.target.populations]  # just take the population, not the slice
            else:
                assert isinstance(projection.target, nineml.Population)
                target_populations = [projection.target]
            for target_population in target_populations:
                if target_population.name in self.psr_map:
                    self.psr_map[target_population.name].add(projection.synaptic_response)
                else:
                    self.psr_map[target_population.name] = set([projection.synaptic_response])

        # create populations
        for population in group.populations.values():
            self._build_population(population, self.assemblies[group.name])
        for selection in group.selections.values():
            self._evaluate_selection(selection, self.assemblies[group.name])

        # create projections
        for projection in group.projections.values():
            self._build_projection(projection, self.assemblies[group.name])

    def _generate_cell_type_and_parameters(self, nineml_population):
        """

        """
        neuron_model = nineml_population.prototype.definition.component
        neuron_namespace = _generate_variable_name(nineml_population.prototype.name)
        synapse_models = {}
        if nineml_population.name in self.psr_map:
            for psr_component in self.psr_map[nineml_population.name]:
                synapse_models[_generate_variable_name(psr_component.name)] = psr_component.definition.component
        subnodes = {neuron_namespace: neuron_model}
        subnodes.update(synapse_models)
        combined_model = al.ComponentClass(name=_generate_variable_name(nineml_population.name),
                                           subnodes=subnodes)
        # now connect ports
        connections = []
        for source in [port for port in neuron_model.analog_ports if port.mode == 'send']:
            for synapse_name, syn_model in synapse_models.items():
                for target in [port for port in syn_model.analog_ports if port.mode == 'recv']:
                    connections.append(("%s.%s" % (neuron_namespace, source.name), "%s.%s" % (synapse_name, target.name)))
        for synapse_name, syn_model in synapse_models.items():
            for source in [port for port in syn_model.analog_ports if port.mode == 'send']:
                for target in [port for port in neuron_model.analog_ports if port.mode in ('recv, reduce')]:
                    connections.append(("%s.%s" % (synapse_name, source.name), "%s.%s" % (neuron_namespace, target.name)))
        for connection in connections:
            combined_model.connect_ports(*connection)
        ### HACK ###
        synapse_components = [self._nineml_module.CoBaSyn(namespace=name,  weight_connector='q') for name in synapse_models.keys()]
        celltype_cls = self._nineml_module.nineml_cell_type(
            combined_model.name, combined_model, synapse_components)
        cell_params = resolve_parameters(nineml_population.prototype, self.random_distributions)
        return celltype_cls, cell_params

    #    iaf_2coba_model = al.ComponentClass(
    #        name="iaf_2coba",
    #        subnodes = {"iaf" : iaf.get_component(),
    #                    "cobaExcit" : coba_synapse.get_component(),
    #                    "cobaInhib" : coba_synapse.get_component()} )
    #
    ## Connections have to be setup as strings, because we are deep-copying objects.
    #iaf_2coba_model.connect_ports( "iaf.V", "cobaExcit.V" )
    #iaf_2coba_model.connect_ports( "iaf.V", "cobaInhib.V" )
    #iaf_2coba_model.connect_ports( "cobaExcit.I", "iaf.ISyn" )
    #iaf_2coba_model.connect_ports( "cobaInhib.I", "iaf.ISyn" )

    def _build_population(self, nineml_population, assembly):
            if isinstance(nineml_population.prototype, nineml.SpikingNodeType):
                n = nineml_population.number
                if nineml_population.positions is not None:
                    pyNN_structure = _build_structure(nineml_population.positions.structure)
                else:
                    pyNN_structure = None
                # TODO: handle explicit list of positions
                cell_class, cell_params = self._generate_cell_type_and_parameters(nineml_population)

                p_obj = self.sim.Population(n, cell_class,
                                            cell_params,
                                            structure=pyNN_structure,
                                            initial_values=resolve_parameters(nineml_population.prototype,
                                                                              self.random_distributions,
                                                                              resolve="initial_values"),
                                            label=nineml_population.name)

            elif isinstance(nineml_population.prototype, nineml.Group):
                raise NotImplementedError
            else:
                raise Exception()

            assembly.populations.append(p_obj)

    def _evaluate_selection(self, nineml_selection, assembly):
        assert nineml_selection.evaluated
        new_assembly = self.sim.Assembly(label=nineml_selection.name)
        for population, selector in nineml_selection.populations:
            parent = assembly.get_population(population.name)
            if selector is not None:
                view = eval("parent[%s]" % selector)
                view.label = nineml_selection.name
                new_assembly += view
            else:
                new_assembly += parent
        self.assemblies[nineml_selection.name] = new_assembly
        assembly += new_assembly  # add the contents of the selection assembly to the top-level assembly

    def _build_connector(self, nineml_projection):
        #connector_cls = generate_connector_map()[nineml_projection.rule.definition.url]
        connector_params = resolve_parameters(nineml_projection.rule, self.random_distributions)
        #synapse_parameters = nineml_projection.connection_type.parameters
        #connector_params['weights'] = synapse_parameters['weight'].value
        #connector_params['delays'] = synapse_parameters['delay'].value*scale('delay', synapse_parameters['delay'].unit)
        inline_csa = nineml_projection.rule.definition.component._connection_rule[0]
        cset = inline_csa(*connector_params.values()).cset  # TODO: csa should handle named parameters; handle random params
        return self.sim.CSAConnector(cset)

    def _build_synapse_dynamics(self, nineml_projection):
        #if nineml_projection.connection_type.definition.url != pyNN.nineml.utility.catalog_url + "/connectiontypes/static_synapse.xml":
        #    raise Exception("Dynamic synapses not yet supported by the 9ML-->PyNN converter.")
        # for now, just use static synapse
        ### HACK ###
        return self.sim.StaticSynapse()  # weights, delays?

    def _build_projection(self, nineml_projection, assembly):
        populations = {}
        for p in assembly.populations:
            populations[p.label] = p
        for a in self.assemblies.values():
            if a is not assembly:
                populations[a.label] = a

        connector = self._build_connector(nineml_projection)
        receptor_type = nineml_projection.synaptic_response.name
        assert receptor_type in populations[nineml_projection.target.name].receptor_types
        synapse_dynamics = self._build_synapse_dynamics(nineml_projection)

        prj_obj = self.sim.Projection(populations[nineml_projection.source.name],
                                      populations[nineml_projection.target.name],
                                      connector,
                                      receptor_type=receptor_type,
                                      synapse_type=synapse_dynamics,
                                      label=nineml_projection.name)
        self.projections[prj_obj.label] = prj_obj  # need to add assembly label to make the name unique

    def describe(self):
        description = "Network model generated from a 9ML description, consisting of:\n  "
        description += "\n  ".join(a.describe() for a in self.assemblies.values()) + "\n"
        description += "\n  ".join(prj.describe() for prj in self.projections.values())
        return description


if __name__ == "__main__":
    # For testing purposes: read in the network and print its description
    # if using the nineml or neuroml backend, re-export the network as XML (this doesn't work, but it should).
    import sys, os
    from pyNN.utility import get_script_args
    nineml_file, simulator_name = get_script_args(2, "Please specify the 9ML file and the simulator backend.")
    exec("import pyNN.%s as sim" % simulator_name)

    sim.setup(filename="%s_export.xml" % os.path.splitext(nineml_file)[0])
    network = Network(sim, nineml_file)
    print network.describe()
    sim.end()
