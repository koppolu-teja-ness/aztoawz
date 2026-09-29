param vnetName string = 'vnet-demo'
param subnetName string = 'subnet-app'
param location string = resourceGroup().location

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.10.0.0/16'
      ]
    }
  }
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' = {
  name: '${vnetName}/${subnetName}'
  dependsOn: [
    vnet
  ]
  properties: {
    addressPrefix: '10.10.1.0/24'
    serviceEndpoints: [
      {
        service: 'Microsoft.Storage'
      }
    ]
  }
}

module diagnostics './diagnostics_module.bicep' = {
  name: 'diag-module'
}
