Imports App.Util

Namespace App
    Module Service
        Function Run(ByVal s As String) As Boolean
            Return Validator.IsValid(s)
        End Function
    End Module
End Namespace
